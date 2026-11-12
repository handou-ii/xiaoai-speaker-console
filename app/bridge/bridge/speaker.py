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



# 云端合成（edge-tts）可选音色。id 直接就是 edge-tts 的 voice 名，
# 界面上只显示 name + desc，改成别的值一律按默认音色处理。
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
EDGE_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓", "desc": "女声 · 自然亲切（默认）"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊", "desc": "女声 · 偏活泼"},
    {"id": "zh-CN-YunxiNeural", "name": "云希", "desc": "男声 · 年轻"},
    {"id": "zh-CN-YunjianNeural", "name": "云健", "desc": "男声 · 浑厚有力"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬", "desc": "男声 · 新闻播报腔"},
    {"id": "zh-CN-YunxiaNeural", "name": "云夏", "desc": "男声 · 少年感"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北", "desc": "女声 · 东北话"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮", "desc": "女声 · 陕西话"},
    {"id": "zh-HK-HiuMaanNeural", "name": "曉曼", "desc": "女声 · 粤语"},
    {"id": "zh-TW-HsiaoChenNeural", "name": "曉臻", "desc": "女声 · 台湾腔"},
]
EDGE_VOICE_IDS = {v["id"] for v in EDGE_VOICES}


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
        self.voice = cfg.speak.get("edge_voice", DEFAULT_VOICE)
        self.max_seg = int(cfg.speak.get("max_seg_chars", 60))
        # 播报总字数上限，0 = 不限制。OpenClaw 侧已有「超过 100 字自己总结」的规则，
        # 这里再按字数砍会把整段丢掉：2026-11-11 实测一条三段共 167 字的回复，
        # 被截成 65 字播出，末段「要我现在切模型吗」的问句整个消失。
        try:
            self.max_total = int(cfg.speak.get("max_say_chars") or 0)
        except (TypeError, ValueError):
            self.max_total = 0
        # 单段合成超时。edge-tts 偶尔会卡住几十秒才报 "No audio was received"，
        # 这段时间音箱会把上一段播报循环重放（实测 42 秒），必须自己卡死上限。
        self.tts_timeout = float(cfg.speak.get("tts_timeout") or 12)
        # 播报前把长回答压成一句口播。shrinker 由 main 注入（agent.shrink），
        # 这样 speaker 不认识 agent，也不会和它循环引用。
        # 为什么需要：agent 侧 system 有 4.7 万字符、会话堆了 300 多条历史，
        # 「不超过60字」那句被淹没，实测照样回 167 字（2026-11-11）。
        self.shrinker = None
        try:
            self.shrink_over = int(cfg.speak.get("shrink_over", 80))
        except (TypeError, ValueError):
            self.shrink_over = 80
        try:
            self.shrink_chars = int(cfg.speak.get("shrink_chars", 60))
        except (TypeError, ValueError):
            self.shrink_chars = 60
        # 打断点与播报时长估算（毫秒），可在 config.timing 里按机型微调
        self.timing = (cfg.get("timing") or {})
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
        self._hold_task = None        # 持续压制小爱原生回答的任务
        self._hold_stop = False       # 置真后压制循环立刻收手
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

    def _ms(self, key, default_ms):
        """从 config.timing 读毫秒值，读不到/非法就用默认"""
        try:
            return max(0.0, float(self.timing.get(key, default_ms))) / 1000.0
        except (TypeError, ValueError):
            return default_ms / 1000.0

    def _stop_plan(self):
        """停止序列：第一发 pause（比 stop 早约 0.3 秒停下），后面几发 stop 兜底。

        config.timing.stop_plan_ms 可以改时机，例如 [0, 300, 800]。
        """
        raw = self.timing.get("stop_plan_ms")
        plan = []
        if isinstance(raw, (list, tuple)):
            for v in list(raw)[:4]:
                try:
                    delay = max(0.0, float(v)) / 1000.0
                except (TypeError, ValueError):
                    continue
                # 第一发必须是 pause：即使前面有脏值被跳过，也不能变成 stop 打头
                plan.append((delay, "pause" if not plan else "stop"))
        return tuple(plan) or ((0.0, "pause"), (0.4, "stop"), (1.0, "stop"))

    async def stop_burst(self, plan=None):
        """连发几次控制指令，专治「小爱开口了但控制台还没打断」。

        小米的对话记录是在回答生成完之后才写入的：我们拿到 query 时，小爱
        往往已经念出前几个字。这时单发一次 stop 有两种落空方式——
        ① 设备还没真正开播，指令落在空处，之后它照念；
        ② 指令丢了或延迟到，等它到达已经过去一两句。

        第一发用 pause 而不是 stop：同一台设备上实测 pause 往返约 0.60 秒、
        stop 约 0.88 秒（都是走小米的远程代理），pause 能让声音早约 0.3 秒停下；
        后面两发 stop 兜住「pause 没落实」的情况，同时把播放状态清干净。

        每次发之前看 _audio_gen 有没有变：play_url 一推新音频就 +1，
        说明已经进入「播自己的东西」阶段，立刻收手，绝不把刚起的音乐停掉。
        """
        plan = plan or self._stop_plan()
        gen = self._audio_gen

        async def once(d, action):
            if d:
                await asyncio.sleep(d)
            if self._audio_gen != gen:
                log.info("停止序列中止：已经推送新音频")
                return
            await self._play_action(action)

        # 并发发，不串行：单次往返 0.6~0.9 秒，串行会把第三次推到 2 秒后
        await asyncio.gather(*(once(d, a) for d, a in plan))

    # ---------- 持续压制（按住小爱，直到轮到我们开口） ----------
    def start_hold(self):
        """拿到指令就按住小爱，别让它把原生回答念出来

        小米只开放事后的对话记录接口，拿不到「云端识别出唤醒词」那一刻；
        而这条记录是云端把回答生成完之后先写入、再下发设备播报的，所以我们
        读到时它通常还没出声或刚出声。

        原来只连发三发（0/400/1000ms）就撒手，之后 LLM 要跑十几秒，这中间
        它若晚开口就整句念完、没人拦 —— 用户听到「小爱先说完整一句才被掐」
        多半是这种，而不是三发没生效。

        这里改成持续按：每 hold_interval_ms 发一次 pause，一直发到自己开始
        推音频（_audio_gen 变化）或到 hold_max_ms 上限。
        """
        if self._hold_task and not self._hold_task.done():
            return self._hold_task
        self._hold_stop = False
        self._hold_task = asyncio.create_task(self._hold_loop())
        return self._hold_task

    async def release_hold(self, timeout=2.0):
        """停止压制，并等在飞的 pause 落地

        必须等：晚到的 pause 会把我们刚推上去的播报停掉，等于自己掐自己。
        """
        task, self._hold_stop = self._hold_task, True
        if not task or task.done():
            self._hold_task = None
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("压制没在 %.1fs 内收手，强制取消", timeout)
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        except BaseException:
            pass
        self._hold_task = None

    async def _hold_loop(self):
        """密前疏后：小爱几乎都在前几秒开口，过了这段它已经念完了

        OpenClaw 一轮实测 30~40 秒，全程 0.4 秒一次的密度要发上百次请求，
        没必要，也会被小米限流。所以前 hold_dense_ms（默认 5 秒）用
        hold_interval_ms（0.4 秒）密集按，之后放宽到 hold_sparse_ms（1.5 秒）
        继续兜住晚开口的情况，总上限 hold_max_ms（默认 45 秒）覆盖最长一轮。
        """
        dense = self._ms("hold_interval_ms", 400)
        sparse = self._ms("hold_sparse_ms", 1500)
        dense_sec = self._ms("hold_dense_ms", 5000)
        max_sec = self._ms("hold_max_ms", 45000)
        gen = self._audio_gen
        t0 = time.time()
        pending, sent = set(), 0
        log.info("开始压制小爱原生回答（%.1fs 内每 %.2fs，其后每 %.2fs，上限 %.0fs）",
                 dense_sec, dense, sparse, max_sec)

        async def fire():
            nonlocal sent
            try:
                # 单次给 1 秒上限：卡住的请求会拖慢收手，而收手慢就会掐到自己的播报
                await asyncio.wait_for(
                    self.auth.mina.player_pause(self.device_id), timeout=1.0)
                sent += 1
            except Exception as e:
                log.debug("压制 pause 失败：%s", e)

        try:
            while not (self._hold_stop or self._audio_gen != gen):
                elapsed = time.time() - t0
                if elapsed >= max_sec:
                    log.info("压制到上限 %.0fs 退出（还没轮到我们播报）", max_sec)
                    break
                interval = dense if elapsed < dense_sec else sparse
                task = asyncio.create_task(fire())
                pending.add(task)
                task.add_done_callback(pending.discard)
                # 细粒度等待：外部要求收手时最多 50ms 就能反应，不用等满一个间隔
                waited = 0.0
                while (waited < interval and not self._hold_stop
                       and self._audio_gen == gen):
                    await asyncio.sleep(0.05)
                    waited += 0.05
        finally:
            # 等在飞的请求落地，否则它们会停掉我们随后推出的播报
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            log.info("压制结束：%.1fs 内落地 %d 次 pause", time.time() - t0, sent)

    async def _play_action(self, action):
        try:
            ret = await self.auth.mina.ubus_request(
                self.device_id, "player_play_operation", "mediaplayer",
                {"action": action, "media": "app_ios"})
            log.info("player_%s -> %s", action, (ret or {}).get("code"))
            return ret
        except Exception as e:
            log.warning("player_%s 失败: %s", action, e)
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

    # ---------- 播报音量 ----------
    def resolve_voice(self):
        """当前音色：每次现读配置，值不认识就用默认音色"""
        v = str(self.cfg.speak.get("edge_voice") or DEFAULT_VOICE).strip()
        return v if v in EDGE_VOICE_IDS else DEFAULT_VOICE

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
        # 长回答先压成一句口播。放在 release_hold 之前：
        # 压缩这几秒压制还在跑，小爱仍被按住，顺便补上原先的收手空窗。
        if self.shrinker is not None and self.shrink_over > 0 \
                and len(text) > self.shrink_over:
            raw_len = len(text)
            try:
                brief = await self.shrinker(text, self.shrink_chars)
            except Exception as e:
                log.warning("口播压缩失败，按原文播：%s", e)
                brief = ""
            if brief and len(brief) < raw_len:
                log.info("口播压缩 %d 字 → %d 字", raw_len, len(brief))
                text = brief
        # 先收手：压制还在跑的话，这一句刚出口就会被自己发的 pause 掐掉。
        # 原生 TTS 不走 play_url，所以这一步必须放在这里，不能只放 play_url。
        await self.release_hold()
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
            # 直接拼接：每段本身以句末标点结尾，再加逗号会出现「了。，你」这种双标点
            tail = "".join(s for s in segs[1:] if s)
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
        # 这几个时间量不同机型/网络下不一样，全部走 config.timing：
        #   speech_char_ms  每字估算毫秒、speech_base_ms 固定开销、speech_min_ms 下限
        #   speech_margin_ms 估算外的余量、start_wait_ms 等进入播放态的上限、poll_ms 轮询间隔
        est = max(self._ms("speech_min_ms", 1200),
                  len(text) * self._ms("speech_char_ms", 260)
                  + self._ms("speech_base_ms", 1000))
        t0 = time.time()
        deadline = t0 + est + self._ms("speech_margin_ms", 2500)
        started = False
        while time.time() < deadline:
            playing = await self.is_playing()
            if playing is True:
                started = True
            elif playing is False and started:
                log.info("原生播报念完（%.1fs）", time.time() - t0)
                return
            if not started and time.time() - t0 > self._ms("start_wait_ms", 2000):
                break                      # 一直没进入播放态，不再空等
            await asyncio.sleep(self._ms("poll_ms", 200))
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

        # 音色每次现读配置：换音色不用重启容器，和 edge_volume 一个道理
        voice = self.resolve_voice()
        # 音量与静音垫都进缓存键：改过播报增益或垫长之后，旧文件不能继续复用，
        # 否则会出现「设置改了但声音没变」的假象。
        vol = str(self.cfg.speak.get("edge_volume") or "+100%")
        name = hashlib.md5(("%s|%s|%s|g1|pad%d" % (text, voice, vol, PAD_TAIL))
                           .encode()).hexdigest()[:16]
        path = os.path.join(self.static_dir, f"{name}.mp3")
        if os.path.isfile(path) and os.path.getsize(path) > 1024:
            return path
        # 先写 .part，成功后再原子改名：否则中途失败会留下半截文件，
        # 下次被当成"已缓存"直接播，永远错下去。
        tmp = f"{path}.part"
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(text, voice, volume=vol).save(tmp),
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

    # ---------- 应答音：先应一声，别让用户以为没听见 ----------
    async def confirm_then_hold(self, filename):
        """播一句极短的应答音，播完把压制接回来

        为什么需要：走 OpenClaw 时一轮要 12~15 秒才出声，这段时间小爱被
        pause 按住、我们也没声音，用户听到的是纯静默，会以为没唤醒、于是
        重复喊一遍，把上一轮直接顶掉（云端记录里同一个问题连问 5 次就是
        这么来的）。推一段预置 mp3 占住音频通道，既填了静默，也顺带把
        小爱可能漏出来的开头几个字顶掉。

        注意两点：
        - play_url 内部会 release_hold 并让 _audio_gen +1，压制循环随之收手，
          所以播完必须重新 start_hold，否则 agent 那 10 秒全程没人压。
        - 播的这 1~2 秒里若正式回答已经回来（gen 又变了），就不要再接压制，
          否则会把刚推上去的播报掐掉。
        """
        if not filename:
            return
        path = os.path.join(self.static_dir, filename)
        if not os.path.isfile(path):
            log.warning("应答音不存在，跳过：%s", path)
            return
        gen = self._audio_gen
        await self.play_url(self.media_url(filename), interrupt=False)
        dur = mp3_duration(path) or (1.0 + PAD_TAIL)
        # ★ 小爱推 URL 默认 loop=1（单曲循环），短音频播完不会自己停 —— 不掐就会
        # 把「好的，稍等」念到天荒地老（2026-11-11 17:35 实测）。这里和 say() 一样：
        # 音频尾部带 PAD_TAIL 秒静音，按"总长 − 静音"掐断，掐断点落在静音里，
        # 既不会漏出第二遍开头的字，也不会截断语音。
        await self._wait_speech(dur - PAD_TAIL)
        await self.stop()
        if self._audio_gen != gen + 1:
            log.info("应答音播完，但已有新音频，不再接压制")
            return
        self.start_hold()
        log.info("应答音播完（%.1fs），继续压制", dur)

    async def play_url(self, url, interrupt=True):
        # 先收手再推流：压制期间在飞的 pause 晚到会把这条播报掐掉
        await self.release_hold()
        self._audio_gen += 1          # 通知压制/连发：已经进入播自己的东西，别再发 stop
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
