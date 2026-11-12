"""小爱 ↔ 本地曲库 ↔ OpenClaw 语音桥 + WebUI

处理顺序：自定义指令 → 内置指令（本地）→ OpenClaw（只处理 NAS 类问题）。
播放不依赖 OpenClaw，它只是"可选的大脑"。

用法：
    python main.py --config config.json            # 正常监听 + WebUI
    python main.py --config config.json --devices  # 打印设备列表，用于填配置
    python main.py --config config.json --simulate "让小龙虾看一下NAS状态"
"""
import argparse
import asyncio
import logging
import logging.handlers
import os
import pathlib
import time

from aiohttp import web

from bridge.agent import Agent
from bridge.auth import Auth
from bridge.config import Config
from bridge.history import History
from bridge.listener import ConversationListener
from bridge.llm import Llm
from bridge.memory import Memory
from bridge.tagging import Tagger
from bridge.mi_login import MiLogin
from bridge.music import MusicLibrary
from bridge.player import Player
from bridge.router import Router
from bridge.rules import WakeMatcher, normalize, strip_device_wake
from bridge.speaker import Speaker
from bridge import webui

log = logging.getLogger("main")

BASE_DIR = pathlib.Path(__file__).resolve().parent


def setup_logging(verbose=False, log_file=None):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-9s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    # miservice 会把每一次 ubus 请求都打成 INFO，看门狗轮询会刷满日志
    logging.getLogger("miservice").setLevel(logging.WARNING)
    # 登录失败时 miservice 会打一条 ERROR + 10 行 traceback。我们自己已经记了
    # 失败次数和冷却，这里只留 CRITICAL，避免再被刷屏（2026-10-29 曾刷 3 万条）。
    logging.getLogger("miservice.miaccount").setLevel(logging.CRITICAL)


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.auth = None
        self.speaker = None
        self.agent = None
        self.llm = None
        self.library = None
        self.tagger = None
        self.player = None
        self.listener = None
        self.router = None
        self.mi_login = None
        self.memory = None
        self.history = History()
        self.matcher = WakeMatcher(cfg)
        self._busy = False

    async def start(self):
        self.auth = await Auth(self.cfg).start()
        self.speaker = Speaker(self.auth, self.cfg)
        self.agent = Agent(self.auth, self.cfg)
        # 播报前把长回答压成一句口播。agent 侧 system 有 4.7 万字符、会话堆了
        # 300 多条历史，「不超过60字」那句会被长回复的历史示范盖过（2026-11-11
        # 实测同样一句约束，直连模型 3.7 秒回 64 字，走 agent 回 167 字）。
        # 在播报口兜一道，不依赖 agent 听不听话。
        self.speaker.shrinker = self.agent.shrink
        self.llm = Llm(self.cfg)
        self.library = MusicLibrary(self.cfg)
        self.tagger = Tagger(self.cfg, self.library, self.llm)
        self.player = Player(self.speaker, self.library, self.cfg)
        # 播放历史与偏好：数据落 conf/memory.db（@appdata，升级不丢）。
        # 初始化失败也只是没有记忆，不影响放歌。
        self.memory = Memory(self.cfg)
        self.player.memory = self.memory
        self.router = Router(self)
        self.mi_login = MiLogin(self.cfg)
        self.listener = ConversationListener(self.auth, self.cfg)
        return self

    async def scan_library(self, force=False):
        n = await asyncio.to_thread(self.library.scan, force)
        if force:
            log.info("曲库已重建：%d 首", n)
        return n

    async def _library_watch(self):
        """曲库目录巡检：发现新文件就重建索引

        为什么需要它：`scan()` 的实现是「内存里已有曲目就直接返回」，
        只有 force=True 才真的重扫。也就是说服务跑着的时候，往音乐目录
        里新放歌永远不会自己出现，只能手动点「重建曲库索引」或重启。
        这里按固定间隔比一次目录指纹（文件数 + 最近修改时间），变了才扫，
        没变几乎不花时间。
        """
        m = self.cfg.music or {}
        interval = float(m.get("watch_interval") or 300)
        if interval <= 0:
            return
        last = None
        while True:
            try:
                snap = await asyncio.to_thread(self.library.snapshot)
                if last is not None and snap != last:
                    log.info("曲库目录有变化（%s → %s），重建索引", last, snap)
                    n = await asyncio.to_thread(self.library.scan, True)
                    log.info("曲库已重建：%d 首", n)
                    snap = await asyncio.to_thread(self.library.snapshot)
                last = snap
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("曲库巡检失败：%s", e)
                await asyncio.sleep(interval)

    async def show_devices(self):
        devices = await self.auth.device_list() or []
        print(f"\n共 {len(devices)} 台设备：\n")
        for d in devices:
            print(
                f"  name={d.get('name','')!r:24} "
                f"hardware={d.get('hardware',''):10} "
                f"deviceID={d.get('deviceID','')}"
            )
        print("\n把目标设备的 hardware / deviceID 填进 config.json 的 device 节点。\n")

    async def _warmup(self):
        """预热 agent 会话。OpenClaw 没启用就直接跳过——它不影响播放。"""
        if not self.agent.enabled:
            log.info("OpenClaw 未启用，跳过预热（本地播放不受影响）")
            return
        try:
            r = await self.agent.chat("hi")
            log.info("agent 预热完成：%s", (r or "")[:40])
        except Exception as e:
            log.warning("agent 预热失败：%s", e)

    async def _speak(self, text, resume=True):
        """播报一句。resume=True 时播完把音乐接回去（小爱不支持 seek，只能从头重来）"""
        if not text:
            return
        await self.player.pause_for_speech()
        try:
            await self.speaker.say(text)
        finally:
            if resume:
                await self.player.resume_after_speech()

    async def handle(self, rec):
        t0 = time.perf_counter()
        text = rec["query"]
        hit, intent = self.matcher.match(text)
        bare_plan = None
        if not hit:
            # 没叫名字：默认不处理（否则小爱的每句话都会被劫持）。
            # 开了「无唤醒词触发自定义指令」后，只认自定义指令，内置播放/闲聊都不猜。
            bare_plan = self.router.bare_plan(text)
            if not bare_plan:
                log.debug("未命中唤醒词：%s", text)
                return
            intent = strip_device_wake(normalize(text)) or normalize(text)
            log.info("没叫唤醒词，但命中自定义指令「%s」：%s", bare_plan.note, text)
        # 命中唤醒词但后面没内容（「播放本地歌曲」）= 空意图，交给路由按"随便放点"处理；
        # 只有没命中唤醒词（走自定义指令兜底）时才拿整句当意图。
        if not intent and not hit:
            intent = text

        if self._busy:
            log.warning("上一条还在处理，跳过")
            self.history.command(text, intent, reply="（上一条还在处理，已跳过）",
                                 ok=False, stage="busy")
            return
        self._busy = True
        # 处理期间挂起看门狗：它会把上一首拉回来，和新播放打架
        self.player.hold(150)
        try:
            # 掐断小爱自己的回答。它的对话记录是在回答生成完之后才写的，
            # 我们拿到 query 时它往往已经开口，所以这一条必须排在所有 await
            # 之前（原先是算完 plan 才发，白等几十到几百毫秒）。
            #
            # 只连发三发不够：LLM 这轮要跑十几秒，三发全落在前 1 秒内，它若
            # 晚开口（云端 NLP 慢时很常见）就整句念完、没人再拦。改成持续
            # 压制，一直发到自己开始播报为止。
            self.speaker.start_hold()
            plan = bare_plan or await self.router.plan(
                intent, wake_hit=hit,
                wake_scope=getattr(self.matcher, "last_scope", None),
                wake_name=getattr(self.matcher, "last_name", None))
            if bare_plan:
                log.info("指令=%r → %s %s（未带唤醒词）", intent, plan.source, plan.note)
            else:
                log.info("命中「%s」(%s)，指令=%r → %s %s",
                         getattr(self.matcher, "last_name", "") or "?",
                         getattr(self.matcher, "last_scope", "") or "-",
                         intent, plan.source, plan.note)
            item = self.history.command(text, intent, stage="running",
                                        route=plan.source, note=plan.note,
                                        plan=plan.reply)

            confirm = self.cfg.speak.get("confirm_audio")
            t1 = time.perf_counter()

            if plan.handled:
                reply, agent_ms = await self._run_plan(plan)
            else:
                # 等 agent 回答期间先把音乐挂起：否则看门狗看到"没在播"，
                # 会在等待的十几秒里把同一首歌反复重推
                await self.player.pause_for_speech()
                # 要等十几秒的这条路径才应声：本地指令一秒内就有回复，
                # 再垫一句「好的，稍等」反而拖沓。应答音播完会自动把压制
                # 接回来（见 speaker.confirm_then_hold）。
                if confirm:
                    asyncio.create_task(self.speaker.confirm_then_hold(confirm))
                # 压制不在这里收手：这十几秒正是小爱可能开口的窗口，
                # 由 _speak / play_url 在推流前一手收掉（见 speaker.release_hold）
                t_ask = time.time()
                reply = await self.agent.chat(intent)
                agent_ms = int((time.perf_counter() - t1) * 1000)
                # agent 自己会用 speaker_say 播报（prompt 里要求它这么做）。
                # 这一轮它已经说过话，就别再把返回的文本念一遍。
                if reply and self.speaker.last_spoke_at <= t_ask:
                    await self._speak(reply)
                else:
                    await self.player.resume_after_speech()


            t3 = time.perf_counter()
            item.update(reply=reply, ok=True, stage="done",
                        ms=int((t3 - t0) * 1000),
                        agent_ms=agent_ms,
                        say_ms=int((t3 - t1) * 1000) - agent_ms)
            # 记一句上下文：下一轮「他 / 那首 / 再换一首」靠它解析
            if self.memory:
                self.memory.record_turn(text, intent, reply,
                                        action=getattr(plan, "note", "") or "")
            log.info("耗时 总计 %.0fms（%s）", (t3 - t0) * 1000, plan.source)
        except Exception as e:
            log.exception("处理指令失败")
            self.history.command(text, intent, ok=False, stage="error",
                                 reply=f"异常：{e}")
        finally:
            # 兜底收手：正常路径在 play_url 前已收，这里只处理没播报/抛异常的轮次，
            # 免得压制任务在后台空转到上限
            await self.speaker.release_hold()
            self.player.release()
            self._busy = False

    async def _run_plan(self, plan):
        """执行本地计划。返回 (播报文本, 动作耗时ms)"""
        t1 = time.perf_counter()
        # 动作要接管播放时，说完就别把旧队列接回来，否则旧歌会响半秒再被顶掉
        keep = not getattr(plan, "owns_playback", False)
        if plan.reply:
            await self._speak(plan.reply, resume=keep)
        if not plan.action:
            return plan.reply, 0

        r = await plan.action()
        ms = int((time.perf_counter() - t1) * 1000)
        if isinstance(r, dict):
            if r.get("spoken"):
                await self._speak(r["spoken"])
                return r["spoken"], ms
            if r.get("ok") is False and r.get("error"):
                await self._speak(r["error"])
                return r["error"], ms
        return plan.reply, ms

    async def run(self):
        static_dir = self.speaker.static_dir
        os.makedirs(static_dir, exist_ok=True)

        # 预热：agent 首次请求要加载完整上下文（实测 40k+ token），冷启动可达 60s+
        if self.agent.enabled:
            asyncio.create_task(self._warmup())
        # 播放看门狗：整点报时等外部打断后自动续播（可在 config.music 里调）
        wd = self.cfg.music.get("watchdog") or {}
        asyncio.create_task(self.player.start_watchdog(
            interval=float(wd.get("interval", 12)),
            grace=float(wd.get("grace", 20)),
        ))
        # 音乐库索引（后台建，避免阻塞启动）
        asyncio.create_task(self.scan_library())
        # 曲库目录巡检：新歌放进去要能被发现（scan 本身不会自己重扫，见注释）
        asyncio.create_task(self._library_watch())

        app = web.Application()
        web_dir = str(BASE_DIR / "web")
        webui.attach(app, self, web_dir)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner,
            self.cfg.server.get("host", "0.0.0.0"),
            int(self.cfg.server.get("port", 8790)),
        )
        await site.start()
        log.info(
            "服务已启动 %s:%s，WebUI 与音频地址均为 %s（OpenClaw：%s）",
            self.cfg.server.get("host"),
            self.cfg.server.get("port"),
            self.cfg.speak.get("public_base") or f"127.0.0.1:{self.cfg.server.get('port', 8790)}",
            self.agent.base or "未配置（仅本地播放）",
        )

        try:
            await self.listener.run(self.handle)
        finally:
            await runner.cleanup()
            await self.auth.close()


async def amain(args):
    cfg = Config(args.config)
    setup_logging(args.verbose, cfg.abspath(cfg.get("log_file", default="")))
    bridge = Bridge(cfg)
    await bridge.start()

    if args.devices:
        await bridge.show_devices()
        await bridge.auth.close()
        return

    if args.simulate:
        await bridge.handle({"query": args.simulate, "time": 0, "answer": ""})
        await bridge.auth.close()
        return

    await bridge.run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--devices", action="store_true", help="打印设备列表")
    ap.add_argument("--simulate", metavar="TEXT", help="模拟一句语音指令")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
