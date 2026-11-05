"""播放队列与状态机

agent 只负责"点火"（play_local），后续逐首推进由这里维持，
所以 speaker_play_local 必须立即返回，不能等播完。
"""
import asyncio
import logging
import os
import time

from .audio import duration_of

log = logging.getLogger("player")


class Player:
    def __init__(self, speaker, library, cfg):
        self.speaker = speaker
        self.lib = library
        self.cfg = cfg
        self.queue = []
        self.cursor = -1
        self.timer = None
        self.mode = (cfg.music or {}).get("mode") or "shuffle"
        self.speaking = False
        self._resume_at = None
        self._last_push = 0.0
        self._paused = False
        self._speech_paused = False   # 这次暂停是不是"为了播报"临时按的

    # ---------- 对外 ----------
    async def play_local(self, query, mode="shuffle", limit=30):
        if not query:
            return {"ok": False, "error": "缺少查询词"}
        tracks = self.lib.pick(query, mode, limit)
        if not tracks:
            # 找不到就重扫一次再试：这样「下载完立刻播」不需要手动重建索引
            # （agent 下载的新文件都在 music.roots 下，重扫就能看到）
            try:
                n = await asyncio.to_thread(self.lib.scan, True)
                log.info("曲库重扫（%d 首）后重试：%s", n, query)
                tracks = self.lib.pick(query, mode, limit)
            except Exception:
                log.exception("重扫曲库失败")
        if not tracks:
            return {"ok": False, "error": f"本地没找到与「{query}」匹配的歌曲"}
        await self._start(tracks, mode)
        return {"ok": True, "queued": len(tracks), "now": str(tracks[0]),
                "mode": mode}

    async def play_id(self, tid):
        t = self.lib.get(tid)
        if not t:
            return {"ok": False, "error": "曲目不存在"}
        await self._start([t], "one")
        return {"ok": True, "queued": 1, "now": str(t)}

    async def start_tracks(self, tracks, mode="seq"):
        """直接拿一批曲目开播（路由层已经把检索做完了）"""
        if not tracks:
            return {"ok": False, "error": "没有可播放的曲目"}
        await self._start(list(tracks), mode)
        return {"ok": True, "queued": len(tracks), "now": str(tracks[0]),
                "mode": mode}

    async def set_mode(self, mode):
        """切换播放模式。seq=顺序（放完停）、shuffle=随机、loop=循环"""
        if mode not in ("seq", "shuffle", "loop", "one"):
            return {"ok": False, "error": f"未知模式 {mode}"}
        self.mode = mode
        log.info("播放模式 → %s", mode)
        return {"ok": True, "mode": mode}

    async def next(self):
        if not self.queue:
            return {"ok": False, "error": "队列为空"}
        self._paused = False
        self._resume_at = None
        self.cursor += 1
        if self.cursor >= len(self.queue):
            if self.mode in ("shuffle", "loop"):
                self.cursor = 0
            else:
                return await self.stop("播完了")
        return await self._play_current()

    async def prev(self):
        if not self.queue:
            return {"ok": False, "error": "队列为空"}
        self._paused = False
        self._resume_at = None
        self.cursor = max(0, self.cursor - 1)
        return await self._play_current()

    async def stop(self, reason=""):
        self._cancel_timer()
        self.queue = []
        self.cursor = -1
        await self.speaker.stop()
        return {"ok": True, "stopped": True, "reason": reason}

    async def jump(self, index):
        """跳到队列第 index 首（0 基）"""
        try:
            index = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "error": "index 不合法"}
        if not (0 <= index < len(self.queue)):
            return {"ok": False, "error": "超出队列范围"}
        self.cursor = index
        self._paused = False
        self._speech_paused = False
        self._resume_at = None
        return await self._play_current()

    async def pause(self, reason="暂停"):
        """暂停：记住位置，由 resume 续上（小爱不支持 seek，只能从本曲开头重来）"""
        if not self.queue or self.cursor < 0:
            return {"ok": False, "error": "队列为空"}
        if self._paused:
            return {"ok": True, "paused": True, "index": self.cursor + 1}
        self._resume_at = self.cursor
        self._paused = True
        self._cancel_timer()
        await self.speaker.stop()
        log.info("暂停（%s），待恢复到第 %d 首", reason, self.cursor + 1)
        return {"ok": True, "paused": True, "index": self.cursor + 1}

    async def resume(self, reason=""):
        if not self.queue or self.cursor < 0:
            return {"ok": False, "error": "队列为空"}
        if not self._paused:
            # 没标记暂停却没在播，通常是被外部停了（整点报时/别处点播），直接重新推流
            return await self._play_current()
        if self._resume_at is not None:
            self.cursor = self._resume_at
        self._resume_at = None
        self._paused = False
        await asyncio.sleep(0.3)
        r = await self._play_current()
        log.info("恢复播放第 %d 首 %s", self.cursor + 1, reason)
        return r

    def queue_list(self):
        out = []
        for i, t in enumerate(self.queue):
            d = t.as_dict()
            d["index"] = i
            d["current"] = i == self.cursor
            out.append(d)
        return out

    def status(self):
        cur = self.queue[self.cursor] if 0 <= self.cursor < len(self.queue) else None
        return {
            "playing": bool(cur) and not self._paused,
            "paused": self._paused,
            "now": str(cur) if cur else "",
            "title": cur.name if cur else "",
            "artist": cur.artist if cur else "",
            "album": cur.album if cur else "",
            "id": cur.tid if cur else "",
            "index": self.cursor + 1,
            "total": len(self.queue),
            "mode": self.mode,
            "upcoming": [str(t) for t in self.queue[self.cursor + 1:self.cursor + 4]],
        }

    # ---------- 内部 ----------
    async def _start(self, tracks, mode):
        self._cancel_timer()
        self.queue = tracks
        self.cursor = 0
        self.mode = mode
        await self._play_current()

    async def _play_current(self):
        t = self.queue[self.cursor]
        self._paused = False
        url = self.speaker.media_url_for_track(t)
        await self.speaker.play_url(url, interrupt=True)
        self._last_push = time.time()
        dur = duration_of(t.path) or 200
        self._set_timer(max(3.0, dur - 0.3))
        log.info("播放 [%d/%d] %s (%.0fs)", self.cursor + 1, len(self.queue), t, dur)
        return {"ok": True, "now": str(t), "index": self.cursor + 1,
                "total": len(self.queue)}

    def _set_timer(self, sec):
        self._cancel_timer()
        self.timer = asyncio.create_task(self._auto_next(sec))

    async def _auto_next(self, sec):
        try:
            await asyncio.sleep(sec)
            await self.next()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("自动下一首异常")

    def _cancel_timer(self):
        if self.timer and not self.timer.done():
            self.timer.cancel()
        self.timer = None

    async def pause_for_speech(self):
        """播报前暂停音乐：记住位置，播完由 resume_after_speech 续上

        注意：如果本来就是用户自己按的暂停（_paused 已为真），这里不要接管，
        否则播报结束后会把用户按下的暂停"恢复"成播放。
        """
        self._speech_paused = False
        if not self.queue or not (0 <= self.cursor < len(self.queue)):
            return
        if self._paused:
            return                      # 用户手动暂停中，保持原样
        self._speech_paused = True
        await self.pause("播报")

    async def resume_after_speech(self):
        """播报结束后恢复音乐（小爱不支持 seek，从头播当前曲）

        只恢复"为播报而暂停"的那次；用户手动暂停的不动。
        """
        if not self._speech_paused:
            return
        self._speech_paused = False
        await self.resume("（播报结束）")

    # ---------- 看门狗 ----------
    async def start_watchdog(self, interval=12, grace=20):
        """外部打断（整点报时、小爱别的应答）会导致流被冲掉，这里负责重新推流"""
        while True:
            await asyncio.sleep(interval)
            try:
                if not self.queue or self.cursor < 0 or self._paused:
                    continue
                if time.time() - self._last_push < grace:
                    continue
                playing = await self.speaker.is_playing()
                if playing is False:
                    log.warning("播放被外部打断，自动恢复第 %d 首", self.cursor + 1)
                    await self._play_current()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("看门狗异常")
