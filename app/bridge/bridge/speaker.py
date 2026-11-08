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

# 给每段合成音频补的静音（秒）。小爱把推过去的 URL 当单曲循环放，
# 播报又是靠"到点自己掐断"来收尾，掐断的时机算不准就会把这句话再念一遍。
# 尾巴留一段静音，掐断就落在静音里，声音上听不出来。
# 长度只需覆盖"状态确认 + 掐断指令往返"的抖动，留 6 秒足够，不影响响应速度
# （念完就掐，不会等满静音）。
PAD_TAIL = 6.0


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
        self.voice = cfg.speak.get("edge_voice", "zh-CN-XiaoxiaoNeural")
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
        self._audio_gen = 0           # 每次推新音频 +1，用来让连发 stop 及时收手
        self.silence_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "silence6.mp3")
        self._silence = None
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

    async def stop_burst(self, delays=(0.0, 0.25, 0.6)):
        """连发几次 stop，专治「小爱开口了但控制台还没打断」。

        小米的对话记录是在回答生成完之后才写入的：我们拿到 query 时，小爱
        往往已经念出前几个字。这时单发一次 stop 有两种落空方式——
        ① 设备还没真正开播，指令落在空处，之后它照念；
        ② 指令丢了或延迟到，等它到达已经过去一两句。
        所以按 0 / 0.25 / 0.6 秒连发三次，覆盖这段窗口。

        每次发之前看 _audio_gen 有没有变：play_url 一推新音频就 +1，
        说明已经进入「播自己的东西」阶段，立刻收手，绝不把刚起的音乐停掉。
        """
        gen = self._audio_gen

        async def once(d):
            if d:
                await asyncio.sleep(d)
            if self._audio_gen != gen:
                log.info("停止序列中止：已经推送新音频")
                return
            await self.stop()

        # 并发发，不串行：单次 stop 往返要 0.3~0.8 秒，串行会把第三次推到 1.5 秒后
        await asyncio.gather(*(once(d) for d in delays))

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

    # ---------- 播报音量 ----------
    def speech_volume(self):
        """播报时希望达到的设备音量，0 表示不干预"""
        try:
            return max(0, min(100, int(self.cfg.speak.get("speech_volume") or 0)))
        except (TypeError, ValueError):
            return 0

    async def _raise_volume_for_speech(self):
        """播报前把音量顶到设定值，返回原音量（None = 不需要还原）

        只抬不降：用户自己开的比设定值高就不动，避免播报把音乐音量压下去。
        """
        target = self.speech_volume()
        if target <= 0:
            return None
        # 用短缓存而不是强制刷新：WebUI 每 2 秒轮询，缓存里基本就是最新值，
        # 省下的 0.8 秒直接体现在指令响应上；缓存过期时 get_state 会自己回源。
        st = await self.get_state(ttl=3.0)
        cur = st.get("volume")
        if cur is None:
            log.info("播报音量：读不到当前音量，跳过抬高")
            return None
        try:
            cur = int(cur)
        except (TypeError, ValueError):
            return None
        if cur >= target:
            return None
        r = await self.set_volume(target)
        if not r.get("ok"):
            return None
        log.info("播报音量：%s → %s（播完还原）", cur, target)
        return cur

    async def _restore_volume(self, prev):
        if prev is None:
            return
        # 直接还原，不再先查一次状态：查询要 0.8 秒，而它是每条指令的关键路径。
        # 播报期间用户手动改音量的概率很低，被顶回原值时再调一次即可。
        await self.set_volume(prev)
        log.info("播报音量：已还原 %s", prev)

    # ---------- 播报 ----------
    async def say(self, text, interrupt=True):
        """分段合成 + 顺序播放。默认 edge-tts，音色稳定。"""
        text = (text or "").strip()
        if not text:
            return
        # 现读配置：换 TTS 通道不用重启容器，方便两个通道来回对比
        mode = str(self.cfg.speak.get("tts_mode") or "edge")
        if mode == "xiaomi":
            # 原生 TTS 由设备自己合成，音量就是设备当前音量，中间没有推流音频，
            # 所以不需要播报时临时抬音量——抬了反而多出一次 15→28→15 的起伏。
            try:
                ret = await self.auth.mina.text_to_speech(self.device_id, text)
                log.info("小米 TTS ok len=%d", len(text))
                # ★ 必须等它念完再返回：调用方紧接着就要放歌（stop + play_url），
                # 早一步就会把小爱正在念的这句话掐断（2026-11-08 实测）。
                await self._wait_native_done(text)
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

        # ★ 合成全部成功之后再抬音量：合成失败率高的时候，别让音量白抬一趟
        prev_vol = await self._raise_volume_for_speech()
        try:
            for i, (seg, path) in enumerate(zip(segs, paths)):
                if not path:
                    log.warning("第 %d 段合成失败，跳过：%s", i + 1, seg[:20])
                    continue
                dur = mp3_duration(path) or (len(seg) * 0.22 + 0.8 + PAD_TAIL)
                await self.play_url(f"{self.public_base}/media/{os.path.basename(path)}",
                                    interrupt=(interrupt and i == 0))
                # ★ 小爱推 URL 时默认 loop=1（单曲循环），短音频播完不会自己停；
                # 而 player_set_loop(0) 只改 loop_type 字段、状态照样是 playing，
                # 所以只能自己到点掐断。
                #
                # 掐断点必须落在尾巴的静音里，否则会把这句话再念一遍（2026-11-07 实测）。
                # 1.5.4 是「语音时长 − 0.25 秒」固定值，遇上起播延迟就会踩进循环；
                # 1.5.6 改成「总长 − 3 秒」，安全但每条指令多等约 3 秒。
                # 现在先确认设备真的开播再计时，余量只需 0.8 秒，既不截断也不重复。
                await self._wait_speech(dur - PAD_TAIL)
                if i == len(segs) - 1:
                    await self.stop()
        finally:
            await self._restore_volume(prev_vol)
        self.last_spoke_at = time.time()

    async def _wait_speech(self, speech_dur):
        """等到这句念完（再留一点余量），不依赖设备起播延迟

        设备从收到 URL 到真正出声有 0.5~1.5 秒抖动，固定从推流时刻起算
        要么截断、要么白等。这里先用播放状态确认已开播，再按语音本身的时长计时。
        """
        t0 = time.time()
        started = None
        while time.time() - t0 < 3.0:
            if await self.is_playing():
                started = time.time()
                break
            await asyncio.sleep(0.15)
        if started is None:
            # 状态查询失败（设备没回报）：退回按推流时刻 + 一次查询延迟估算
            log.info("播报：未确认到开播状态，按推流时刻估算")
            started = t0 + 0.7
        remain = (speech_dur + 0.4) - (time.time() - started)
        if remain > 0:
            await asyncio.sleep(remain)

    async def _wait_native_done(self, text):
        """等原生播报念完再返回

        原生 TTS 的时长拿不到，但设备状态会变：念的时候 status=1，念完回到 0。
        先确认进入播放态、再等它离开播放态，比按字数硬估准得多。
        万一起播没抓到（例如音乐同时在放，状态一直是 1），退回按字数估算，不会卡死。
        """
        est = max(1.2, len(text) * 0.26 + 1.0)
        t0 = time.time()
        deadline = t0 + est + 2.5
        started = False
        while time.time() < deadline:
            playing = await self.is_playing()
            if playing is True:
                started = True
            elif playing is False and started:
                log.info("原生播报念完（%.1fs）", time.time() - t0)
                return
            if not started and time.time() - t0 > 2.0:
                break                      # 一直没进入播放态，不再空等
            await asyncio.sleep(0.2)
        remain = est - (time.time() - t0)
        if remain > 0:
            await asyncio.sleep(remain)
        log.info("原生播报按字数估算结束（%.1fs，未取到结束状态）", time.time() - t0)

    def _pad_tail(self, path):
        """在合成结果后面接一段静音

        MP3 是逐帧编码的，直接按字节拼接即可；同采样率/声道下解码器能连着放。
        静音文件随代码分发（容器里没有 ffmpeg，没法现生成）。
        """
        if self._silence is None:
            try:
                with open(self.silence_path, "rb") as f:
                    self._silence = f.read()
            except OSError as e:
                log.warning("静音垫缺失（%s），跳过补静音：%s", self.silence_path, e)
                self._silence = b""
        if not self._silence:
            return
        try:
            with open(path, "ab") as f:
                f.write(self._silence)
        except OSError as e:
            log.warning("补静音失败：%s", e)

    async def _tts_file(self, text):
        """合成并缓存 mp3，返回本地路径"""
        import edge_tts

        # 音量与静音垫都进缓存键：改过播报增益或垫长之后，旧文件不能继续复用，
        # 否则会出现「设置改了但声音没变」的假象。
        vol = str(self.cfg.speak.get("edge_volume") or "+100%")
        name = hashlib.md5(("%s|%s|%s|g1|pad%d" % (text, self.voice, vol, PAD_TAIL))
                           .encode()).hexdigest()[:16]
        path = os.path.join(self.static_dir, f"{name}.mp3")
        if os.path.isfile(path) and os.path.getsize(path) > 1024:
            return path
        # 先写 .part，成功后再原子改名：否则中途失败会留下半截文件，
        # 下次被当成"已缓存"直接播，永远错下去。
        tmp = f"{path}.part"
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(text, self.voice, volume=vol).save(tmp),
                timeout=self.tts_timeout)
            if not os.path.isfile(tmp) or os.path.getsize(tmp) < 1024:
                raise RuntimeError("合成结果为空")
            self._pad_tail(tmp)
            os.replace(tmp, path)
            log.info("edge-tts 生成 %s (%d 字，总长 %.2fs)",
                     os.path.basename(path), len(text), mp3_duration(path) or 0)
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
        self._audio_gen += 1          # 通知 stop_burst：已经进入播自己的东西，别再发 stop
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
