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
        self.library = None
        self.player = None
        self.listener = None
        self.router = None
        self.mi_login = None
        self.history = History()
        self.matcher = WakeMatcher(cfg)
        self._busy = False

    async def start(self):
        self.auth = await Auth(self.cfg).start()
        self.speaker = Speaker(self.auth, self.cfg)
        self.agent = Agent(self.auth, self.cfg)
        self.library = MusicLibrary(self.cfg)
        self.player = Player(self.speaker, self.library, self.cfg)
        self.router = Router(self)
        self.mi_login = MiLogin(self.cfg)
        self.listener = ConversationListener(self.auth, self.cfg)
        return self

    async def scan_library(self, force=False):
        n = await asyncio.to_thread(self.library.scan, force)
        if force:
            log.info("曲库已重建：%d 首", n)
        return n

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
        intent = intent or text

        if self._busy:
            log.warning("上一条还在处理，跳过")
            self.history.command(text, intent, reply="（上一条还在处理，已跳过）",
                                 ok=False, stage="busy")
            return
        self._busy = True
        try:
            # 掐断小爱自己的回答。它的对话记录是在回答生成完之后才写的，
            # 我们拿到 query 时它往往已经开口，所以这一条必须排在所有 await
            # 之前（原先是算完 plan 才发，白等几十到几百毫秒），并且连发三次
            # 覆盖「单次 stop 落在它开播之前」的落空情况。
            stop_task = asyncio.create_task(self.speaker.stop_burst())
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
            if confirm:
                asyncio.create_task(
                    self.speaker.play_url(self.speaker.media_url(confirm), interrupt=False)
                )
            t1 = time.perf_counter()

            if plan.handled:
                reply, agent_ms = await self._run_plan(plan)
            else:
                # 等 agent 回答期间先把音乐挂起：否则看门狗看到"没在播"，
                # 会在等待的十几秒里把同一首歌反复重推
                await self.player.pause_for_speech()
                try:
                    t_ask = time.time()
                    reply = await self.agent.chat(intent)
                finally:
                    await stop_task
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
            log.info("耗时 总计 %.0fms（%s）", (t3 - t0) * 1000, plan.source)
        except Exception as e:
            log.exception("处理指令失败")
            self.history.command(text, intent, ok=False, stage="error",
                                 reply=f"异常：{e}")
        finally:
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
