"""音箱控制：掐断、播报、播音频

播报默认只走 edge-tts，避免"小米 TTS 成功 / 超时降级"两套音色来回跳。
长文本自动切段，按实测时长顺序播放（时长由 mutagen 读取，不依赖 ffmpeg）。
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import socket
import time

from .audio import duration_of, sniff_ext

log = logging.getLogger("speaker")

SENT_END = re.compile(r"(?<=[。！？!?；;])")


def lan_ip():
    """本机对外 IP：音箱要按这个地址来拉音频

    用 UDP connect 探路由出口（不会真的发包），拿不到再退回 hostname。
    这样 config 里不必写死 public_base，换机器/换网段都不用改配置。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip:
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def split_text(text, max_len=60, max_total=80):
    """先按总长截断，再按句末标点切分，超长句按逗号/长度硬切"""
    text = text.strip()
    if max_total and len(text) > max_total:
        kept, acc = [], 0
        for sent in SENT_END.split(text):
            if not sent.strip():
                continue
            if acc + len(sent) > max_total and kept:
                break
            kept.append(sent)
            acc += len(sent)
        text = "".join(kept).strip() or text[:max_total]

    out = []
    for sent in SENT_END.split(text):
        sent = sent.strip().strip("，,、")
        if not sent:
            continue
        while len(sent) > max_len:
            cut = sent.rfind("，", 0, max_len)
            cut = cut + 1 if cut >= max_len // 3 else max_len
            out.append(sent[:cut].strip().strip("，,、"))
            sent = sent[cut:].strip().strip("，,、")
        if sent:
            out.append(sent)
    return [s for s in out if s] or [text]



def mp3_duration(path):
    """播报音频时长（秒），失败返回 None"""
    d = duration_of(path, default=0.0)
    return d or None


class Speaker:
    def __init__(self, auth, cfg):
        self.auth = auth
        self.cfg = cfg
        self.device_id = cfg.device.get("device_id", "")
        self.tts_mode = cfg.speak.get("tts_mode", "edge")
        self.voice = cfg.speak.get("edge_voice", "zh-CN-XiaoyiNeural")
        self.max_seg = int(cfg.speak.get("max_seg_chars", 60))
        self.max_total = int(cfg.speak.get("max_say_chars", 80))
        # 单段合成超时。edge-tts 偶尔会卡住几十秒才报 "No audio was received"，
        # 这段时间音箱会把上一段播报循环重放（实测 42 秒），必须自己卡死上限。
        self.tts_timeout = float(cfg.speak.get("tts_timeout") or 12)
        self.static_dir = cfg.abspath(cfg.speak.get("static_dir") or "./media")
        self.public_base = (cfg.speak.get("public_base") or "").rstrip("/")
        if not self.public_base:
            # 启动脚本会按宿主机的局域网 IP 注入（容器内的 IP 音箱访问不到）
            self.public_base = (os.environ.get("XIAOAI_PUBLIC_BASE") or "").rstrip("/")
            if self.public_base:
                log.info("使用启动参数指定对外地址 %s", self.public_base)
        if not self.public_base:
            # 没配就按本机 IP 自动拼，避免出厂就带一个写死的地址
            self.public_base = f"http://{lan_ip()}:{cfg.server.get('port', 8790)}"
            log.info("未配置 speak.public_base，自动使用 %s", self.public_base)
        self._state_cache = None
        self._state_at = 0.0
        self.last_spoke_at = 0.0      # 最近一次真正播报的时间（防止重复念）
        os.makedirs(self.static_dir, exist_ok=True)

    # ---------- 掐断 ----------
    async def stop(self):
        try:
            ret = await self.auth.mina.player_stop(self.device_id)
            log.info("player_stop -> %s", (ret or {}).get("code"))
            return ret
        except Exception as e:
            log.warning("player_stop 失败: %s", e)
            return None

    async def is_playing(self):
        """是否正在播放。注意 status 藏在 data.info 这个 JSON 字符串里，不是 data 直接给"""
        try:
            resp = await self.auth.mina.player_get_status(self.device_id)
            data = (resp or {}).get("data") or {}
            info = data.get("info")
            if isinstance(info, str):
                try:
                    info = json.loads(info)
                except Exception:
                    info = {}
            status = (info or {}).get("status", data.get("status", -1))
            return status == 1
        except Exception as e:
            log.debug("player_get_status 失败：%s", e)
            return None

    # ---------- 音量 / 状态 ----------
    async def get_state(self, ttl=3.0):
        """音箱真实状态。WebUI 每 2 秒轮询，这里做短缓存，别把 ubus 打爆。"""
        now = time.time()
        if self._state_cache and now - self._state_at < ttl:
            return self._state_cache
        state = {"online": False, "status": -1, "volume": None, "loop": None}
        try:
            resp = await self.auth.mina.player_get_status(self.device_id)
            data = (resp or {}).get("data") or {}
            info = data.get("info")
            if isinstance(info, str):
                try:
                    info = json.loads(info)
                except Exception:
                    info = {}
            info = info or {}
            status = info.get("status", data.get("status", -1))
            state = {
                "online": bool((resp or {}).get("code") == 0) and status >= 0,
                "status": status,
                "volume": info.get("volume"),
                "loop": info.get("loop_type"),
            }
        except Exception as e:
            log.debug("get_state 失败：%s", e)
        self._state_cache = state
        self._state_at = now
        return state

    async def set_volume(self, volume):
        volume = max(0, min(100, int(volume)))
        try:
            ret = await self.auth.mina.player_set_volume(self.device_id, volume)
            log.info("player_set_volume %s -> %s", volume, (ret or {}).get("code"))
            self._state_cache = None                       # 让下次读走真值
            return {"ok": True, "volume": volume,
                    "code": (ret or {}).get("code")}
        except Exception as e:
            log.warning("set_volume 失败：%s", e)
            return {"ok": False, "error": str(e)}

    # ---------- 播报 ----------
    async def say(self, text, interrupt=True):
        """分段合成 + 顺序播放。默认 edge-tts，音色稳定。"""
        text = (text or "").strip()
        if not text:
            return
        if self.tts_mode == "xiaomi":
            try:
                ret = await self.auth.mina.text_to_speech(self.device_id, text)
                log.info("小米 TTS ok len=%d", len(text))
                return ret
            except Exception as e:
                log.warning("小米 TTS 失败(%s)，改用 edge-tts", e)

        segs = split_text(text, self.max_seg, self.max_total)
        # 语音场景只需要"两段"：首段短，快速出声；其余合并成一大段，减少 stop/play 次数
        if len(segs) > 2:
            head = segs[0]
            tail = "，".join(s for s in segs[1:] if s)
            segs = [head, tail] if tail else [head]
        log.info("播报 %d 段：%s", len(segs), " | ".join(segs))

        # ★ 先全部合成完，再开始播。
        # 原来是「边合成边播」：播完第 1 段才去合成第 2 段，只要第 2 段慢或失败，
        # 音箱就会把第 1 段当成待播内容反复循环（2026-10-28 14:53 实测被念了 42 秒）。
        paths = await asyncio.gather(*(self._tts_file(s) for s in segs))

        if not any(paths):
            log.warning("所有段合成失败，改用小米原生 TTS 兜底")
            try:
                return await self.auth.mina.text_to_speech(self.device_id, text)
            except Exception as e:
                log.warning("小米 TTS 兜底也失败: %s", e)
                return

        for i, (seg, path) in enumerate(zip(segs, paths)):
            if not path:
                log.warning("第 %d 段合成失败，跳过：%s", i + 1, seg[:20])
                continue
            dur = mp3_duration(path) or (len(seg) * 0.22 + 0.8)
            await self.play_url(f"{self.public_base}/media/{os.path.basename(path)}",
                                interrupt=(interrupt and i == 0))
            # 每一段都要等它真的播完（含最后一段）。之前只等中间段，
            # 调用方紧接着恢复音乐，末尾那半句就被音乐顶掉了。
            last = (i == len(segs) - 1)
            if last:
                # ★ 小爱推 URL 时默认 loop=1（单曲循环），短音频播完不会自己停，
                # 会一直重放那句话（实测 2026-10-28 被念了 40 秒以上）。
                # player_set_loop(0) 改不动它（状态仍是 playing），只能自己到点掐掉。
                await asyncio.sleep(max(0.3, dur - 0.25))
                await self.stop()
            else:
                await asyncio.sleep(max(0.3, dur - 0.15))
        self.last_spoke_at = time.time()

    async def _tts_file(self, text):
        """合成并缓存 mp3，返回本地路径"""
        import edge_tts

        name = hashlib.md5((text + "|" + self.voice).encode()).hexdigest()[:16]
        path = os.path.join(self.static_dir, f"{name}.mp3")
        if os.path.isfile(path) and os.path.getsize(path) > 1024:
            return path
        # 先写 .part，成功后再原子改名：否则中途失败会留下半截文件，
        # 下次被当成"已缓存"直接播，永远错下去。
        tmp = f"{path}.part"
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(text, self.voice).save(tmp),
                timeout=self.tts_timeout)
            if not os.path.isfile(tmp) or os.path.getsize(tmp) < 1024:
                raise RuntimeError("合成结果为空")
            os.replace(tmp, path)
            log.info("edge-tts 生成 %s (%d 字)", os.path.basename(path), len(text))
            return path
        except asyncio.TimeoutError:
            log.warning("edge-tts 超时(%ss)，跳过该段", self.tts_timeout)
        except Exception as e:
            log.warning("edge-tts 合成失败: %s", e)
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return None

    # ---------- 播音频 ----------
    async def play_url(self, url, interrupt=True):
        if interrupt:
            await self.stop()
            await asyncio.sleep(0.2)
        try:
            ret = await self.auth.mina.play_by_url(self.device_id, url)
            log.info("play_by_url %s -> %s", url, (ret or {}).get("code"))
            return ret
        except Exception as e:
            log.warning("play_by_url 失败: %s", e)
            return None

    def media_url(self, filename):
        return f"{self.public_base}/media/{filename}"

    def media_url_for_track(self, track):
        """曲目播放地址：用短 id，避免超长 URL 被设备拒绝

        后缀以小爱能识别的真实格式为准 —— 曲库里存在 .flac 其实是 OggS 的文件，
        按后缀发过去设备可能不认，这里先嗅一眼前 16 字节。
        """
        ext = sniff_ext(track.path) or os.path.splitext(track.path)[1].lower() or ".mp3"
        return f"{self.public_base}/m/{track.tid}{ext}"

    def media_url_for_path(self, path):
        """本地音乐文件的播放地址（token = base64 路径 + 原扩展名）

        小爱靠 URL 后缀判断音频格式，缺后缀会静默不播，所以必须带上。
        """
        ext = os.path.splitext(path)[1].lower() or ".mp3"
        token = base64.urlsafe_b64encode(path.encode("utf-8")).decode().rstrip("=")
        return f"{self.public_base}/audio/{token}{ext}"
