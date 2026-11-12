"""HTTP 路由：WebUI 静态资源 + 控制 API + 音频回传

分三层能力：
  * 播放控制   —— 只依赖小米账号（serviceToken），与 OpenClaw 无关
  * 配置管理   —— 音乐路径、自定义指令、OpenClaw 地址，写回 config.json
  * 诊断       —— 登录态、OpenClaw 连通性、MCP 工具清单、网关日志
"""
import asyncio
import base64
import json
import logging
import os
import re
import time

from aiohttp import web

from .agent import canonical as oc_canonical, host_ip as oc_host_ip
from .rules import (WakeMatcher, names_from_patterns, normalize,
                    strip_device_wake)
from . import security
from .speaker import DEFAULT_VOICE, EDGE_VOICES, EDGE_VOICE_IDS

log = logging.getLogger("webui")

AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg", ".wma")
STARTED_AT = time.time()

# 容器里挂进来的 OpenClaw 侧文件（配置可写，切换后端模型要改它；日志只读）
OC_CONFIG = os.environ.get("OC_CONFIG", "/oc/openclaw.json")
OC_LOG = os.environ.get("OC_LOG", "/oc/gateway.log")
# 这段是给 OpenClaw 侧「宿主进程」跑 MCP 用的路径，随应用装在 @appcenter 下
MCP_HOST_SCRIPT = os.environ.get(
    "MCP_HOST_SCRIPT", "/vol1/@appcenter/xiaoai-console/bridge/mcp_server.py")


def _need_json(fn):
    async def wrapper(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return web.json_response(await fn(body))
    return wrapper


def _mask(obj, depth=0):
    """把疑似凭据的值打码，诊断接口只用于看结构"""
    if depth > 4:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in k.lower() for s in ("token", "secret", "password", "key")):
                out[k] = "***" if v else ""
            else:
                out[k] = _mask(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_mask(v, depth + 1) for v in obj[:10]]
    if isinstance(obj, str) and len(obj) > 80:
        return obj[:80] + "…"
    return obj


def attach(app, bridge, web_dir):
    # 口令校验放在最前面：控制台（8900）与桥接（8790）共用同一枚 cookie
    app.middlewares.append(security.guard)
    cfg = bridge.cfg
    library = bridge.library
    player = bridge.player
    speaker = bridge.speaker
    history = bridge.history
    router = bridge.router
    auth = bridge.auth
    agent = bridge.agent
    llm = bridge.llm
    static_dir = speaker.static_dir

    # ---------- 音频 ----------
    async def media(request):
        name = request.match_info["name"]
        path = os.path.join(static_dir, os.path.basename(name))
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def audio(request):
        """按 base64 路径播放本地音乐，限制在 music.roots 之内"""
        name = request.match_info["name"]
        stem, ext = os.path.splitext(name)
        raw = stem if ext.lower() in AUDIO_EXTS else name
        token = raw + "=" * (-len(raw) % 4)
        try:
            path = base64.urlsafe_b64decode(token.encode()).decode("utf-8")
        except Exception:
            log.warning("audio: bad token")
            raise web.HTTPBadRequest(text="bad token")
        log.info("audio 请求 %s", path)
        real = os.path.realpath(path)
        if not any(real.startswith(os.path.realpath(r) + os.sep)
                   or os.path.dirname(real) == os.path.realpath(r)
                   for r in library.roots):
            log.warning("audio: 越界 %s", real)
            raise web.HTTPForbidden(text="outside music roots")
        if not os.path.isfile(real):
            log.warning("audio: 文件不存在 %s", real)
            raise web.HTTPNotFound()
        return web.FileResponse(real)

    async def short_audio(request):
        """短 id 播放：/m/<tid>.mp3"""
        name = request.match_info["name"]
        tid = os.path.splitext(name)[0]
        track = library.get(tid)
        if not track:
            log.warning("m: 未知曲目 %s", tid)
            raise web.HTTPNotFound()
        log.info("m 请求 %s (%s)", track, track.path)
        return web.FileResponse(track.path)

    # ---------- 播报 / 播放 ----------
    @_need_json
    async def api_say(body):
        text = (body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "文本为空"}
        history.action(f"播报：{text[:40]}", source=body.get("source", "webui"))
        await player.pause_for_speech()
        try:
            await speaker.say(text)
        finally:
            await player.resume_after_speech()
        return {"ok": True}

    @_need_json
    async def api_play_local(body):
        query = (body.get("query") or "").strip()
        mode = body.get("mode") or player.mode or "shuffle"
        limit = int(body.get("limit") or 0)          # 0 = 不限
        scope = (body.get("scope") or "").strip()
        if scope:
            tracks = library.search_in(scope, query, limit,
                                       shuffle=(mode == "shuffle"))
            r = await player.start_tracks(tracks, mode) if tracks else {
                "ok": False, "error": "这个目录里没有匹配的歌曲"}
        else:
            r = await player.play_local(query, mode, limit)
        history.action(f"播放「{query or '全部'}」", ok=bool(r.get("ok")),
                       detail=r.get("error") or f"{r.get('queued')} 首")
        return r

    @_need_json
    async def api_play_id(body):
        r = await player.play_id(body.get("id", ""))
        history.action(f"播放单曲 {body.get('id','')}", ok=bool(r.get("ok")))
        return r

    @_need_json
    async def api_next(_body):
        r = await player.next()
        history.action("下一首", detail=(r or {}).get("now") or (r or {}).get("error"))
        return r

    @_need_json
    async def api_prev(_body):
        r = await player.prev()
        history.action("上一首", detail=(r or {}).get("now") or (r or {}).get("error"))
        return r

    @_need_json
    async def api_stop(_body):
        r = await player.stop("手动停止")
        history.action("停止播放")
        return r

    @_need_json
    async def api_pause(_body):
        return await player.pause("手动暂停")

    @_need_json
    async def api_resume(_body):
        return await player.resume("手动恢复")

    @_need_json
    async def api_jump(body):
        r = await player.jump(body.get("index"))
        history.action(f"跳到第 {body.get('index')} 首",
                       detail=(r or {}).get("now") or (r or {}).get("error"))
        return r

    @_need_json
    async def api_mode(body):
        mode = body.get("mode")
        r = await player.set_mode(mode)
        if r.get("ok"):
            cfg.set("music", "mode", value=mode)
            cfg.save()
            history.action(f"播放模式 → {mode}")
        return r

    @_need_json
    async def api_volume(body):
        if "volume" in body:
            r = await speaker.set_volume(body["volume"])
            history.action(f"音量 → {body['volume']}", ok=bool(r.get("ok")))
            return r
        st = await speaker.get_state(ttl=0)
        return {"ok": True, "volume": st.get("volume")}

    # ---------- 播报音量 ----------
    def _speak_view():
        try:
            sv = int(cfg.speak.get("speech_volume") or 0)
        except (TypeError, ValueError):
            sv = 0
        return {
            "ok": True,
            "speech_volume": sv,
            "edge_volume": str(cfg.speak.get("edge_volume") or "+100%"),
            "edge_voice": str(cfg.speak.get("edge_voice") or DEFAULT_VOICE),
            "tts_mode": str(cfg.speak.get("tts_mode") or "edge"),
            "voices": EDGE_VOICES,
        }

    async def api_speak_get(_request):
        return web.json_response(_speak_view())

    @_need_json
    async def api_speak_save(body):
        """播报音量：speech_volume 是播报时临时抬到多少（0=不动音箱音量）"""
        if "speech_volume" in body:
            try:
                v = int(body["speech_volume"])
            except (TypeError, ValueError):
                return {"ok": False, "error": "播报音量要填 0–100 的数字"}
            if not 0 <= v <= 100:
                return {"ok": False, "error": "播报音量要在 0–100 之间"}
            cfg.set("speak", "speech_volume", value=v)
        if "edge_volume" in body:
            ev = str(body["edge_volume"]).strip()
            if not re.fullmatch(r"[+-]\d{1,3}%", ev):
                return {"ok": False, "error": "增益写成 +0% / +50% 这种格式"}
            cfg.set("speak", "edge_volume", value=ev)
        if "tts_mode" in body:
            m = str(body["tts_mode"]).strip().lower()
            if m not in ("edge", "xiaomi"):
                return {"ok": False, "error": "TTS 通道只能是 edge（云端合成）或 xiaomi（设备本地）"}
            # 走接口改会同时更新内存里的配置，不必重启容器
            cfg.set("speak", "tts_mode", value=m)
        if "edge_voice" in body:
            v = str(body["edge_voice"]).strip()
            if v not in EDGE_VOICE_IDS:
                return {"ok": False, "error": "不认识的音色：%s" % v}
            cfg.set("speak", "edge_voice", value=v)
        cfg.save()
        v = _speak_view()
        history.action("播报设置 → 通道 %s / 音色 %s / 播报音量 %s（合成增益 %s）"
                       % ("云端合成" if v["tts_mode"] == "edge" else "小米原生",
                          v["edge_voice"], v["speech_volume"], v["edge_volume"]))
        return v

    # ---------- 歌曲画像 ----------
    tagger = bridge.tagger

    async def api_tags(_request):
        return web.json_response(tagger.status() if tagger else {"ok": False})

    @_need_json
    async def api_tags_build(body):
        if not tagger:
            return {"ok": False, "error": "画像模块没初始化"}
        r = tagger.start(force=bool(body.get("force")))
        if r.get("ok"):
            history.action("歌曲画像生成已开始", detail="force=%s" % bool(body.get("force")))
        return r

    async def api_tags_one(request):
        """看某首歌被标成什么样（排查用）"""
        tid = (request.query.get("id") or "").strip()
        t = bridge.library.get(tid) if tid else None
        if not t:
            return web.json_response({"ok": False, "error": "没有这首歌"})
        return web.json_response({"ok": True, "track": t.as_dict(),
                                  "tags": tagger.tags_of(t) if tagger else None})

    # ---------- 打赏 ----------
    # config.json 是单文件挂载，conf/ 才是整目录挂载 → 收款码必须放 conf 下
    DONATE_IMG = cfg.abspath("./conf/donate.png")

    async def api_donate(_request):
        return web.json_response({"ok": True,
                                  "has": os.path.isfile(DONATE_IMG)})

    async def api_donate_qr(_request):
        if not os.path.isfile(DONATE_IMG):
            raise web.HTTPNotFound()
        return web.FileResponse(DONATE_IMG)

    # ---------- AI 模型（直连 OpenAI 兼容端点） ----------
    def _llm_view():
        d = llm.status() if llm else {"enabled": False, "configured": False}
        d["ok"] = True
        return d

    async def api_llm_get(_request):
        return web.json_response(_llm_view())

    @_need_json
    async def api_llm_save(body):
        """base_url / 模型名 / 密钥 / 开关。密钥留空 = 不修改，clear_key 才清空"""
        if "enabled" in body:
            cfg.set("llm", "enabled", value=bool(body["enabled"]))
        if "base_url" in body:
            cfg.set("llm", "base_url", value=str(body["base_url"]).strip())
        if "model" in body:
            cfg.set("llm", "model", value=str(body["model"]).strip())
        if body.get("clear_key"):
            cfg.set("llm", "api_key", value="")
        elif str(body.get("api_key") or "").strip():
            cfg.set("llm", "api_key", value=str(body["api_key"]).strip())
        cfg.save()
        llm.apply()
        history.action("AI 模型配置已保存：%s / %s"
                       % (llm.model or "未填模型", llm.base_raw or "未填地址"))
        return _llm_view()

    @_need_json
    async def api_llm_test(body):
        if not llm.configured:
            return {"ok": False, "error": "先填 base_url 与模型名再测"}
        r = await llm.test(str(body.get("text") or "只回两个字：通了"))
        return r

    # ---------- 曲库 / 音乐路径 ----------
    @_need_json
    async def api_search(body):
        tracks = library.search(body.get("query", ""), int(body.get("limit", 30)))
        return {"ok": True, "count": len(tracks),
                "tracks": [t.as_dict() for t in tracks]}

    async def api_artists(_request):
        library.scan()
        counts = {}
        for t in library.tracks:
            key = t.artist or "未知"
            counts[key] = counts.get(key, 0) + 1
        items = sorted(counts.items(), key=lambda kv: -kv[1])
        return web.json_response({
            "ok": True, "count": len(items),
            "artists": [{"name": n, "count": c} for n, c in items],
        })

    @_need_json
    async def api_rescan(_body):
        n = await bridge.scan_library(force=True)
        history.action(f"重建曲库索引：{n} 首")
        return {"ok": True, "count": n}

    async def api_roots(_request):
        return web.json_response({"ok": True, "roots": library.roots_info()})

    @_need_json
    async def api_roots_add(body):
        r = library.add_root(body.get("path", ""), force=bool(body.get("force")))
        if r.get("ok"):
            n = await bridge.scan_library(force=True)
            r["count"] = n
            history.action(f"新增音乐目录 {body.get('path')}（{n} 首）")
        return r

    @_need_json
    async def api_roots_del(body):
        r = library.remove_root(body.get("path", ""))
        if r.get("ok"):
            n = await bridge.scan_library(force=True)
            r["count"] = n
            history.action(f"移除音乐目录 {body.get('path')}")
        return r

    # ---------- 小米账号 ----------
    async def api_mi(_request):
        return web.json_response({
            "ok": True,
            "account": auth.status(),
            "login": bridge.mi_login.status(),
        })

    @_need_json
    async def api_mi_qr(_body):
        r = await bridge.mi_login.start()
        return r

    @_need_json
    async def api_mi_refresh(_body):
        """立即用 passToken 换一次 serviceToken（账号页 / 排查用）"""
        before = auth.service_token
        ok = await auth._refresh_once("手动")
        st = auth.status()
        return {"ok": bool(ok),
                "changed": bool(ok) and auth.service_token != before,
                "user_id": st.get("user_id", ""),
                "error": "" if ok else st.get("auth_error", ""),
                "token_updated": st.get("token_updated", 0)}

    async def api_mi_qr_poll(_request):
        st = bridge.mi_login.status()
        if st["state"] == "ok" and not getattr(bridge, "_mi_applied", False):
            await auth.reload()
            bridge._mi_applied = True
            history.action(f"小米账号登录成功（{st['user_id']}）")
        elif st["state"] != "ok":
            bridge._mi_applied = False
        return web.json_response({"ok": True, **st, "account": auth.status()})

    @_need_json
    async def api_mi_password(body):
        r = await auth.login_password(body.get("account", "").strip(),
                                      body.get("password", ""))
        history.action("账号密码登录", ok=bool(r.get("ok")),
                       detail=r.get("error") or r.get("user_id"))
        return r

    @_need_json
    async def api_mi_devices(_body):
        try:
            devices = await auth.device_list() or []
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "devices": [
            {"name": d.get("name", ""), "hardware": d.get("hardware", ""),
             "device_id": d.get("deviceID", ""),
             "current": d.get("deviceID") == cfg.device.get("device_id")}
            for d in devices]}

    @_need_json
    async def api_mi_device(body):
        """切换目标音箱"""
        did = (body.get("device_id") or "").strip()
        hw = (body.get("hardware") or "").strip()
        if not did:
            return {"ok": False, "error": "缺少 device_id"}
        cfg.set("device", "device_id", value=did)
        if hw:
            cfg.set("device", "hardware", value=hw)
        if body.get("name"):
            cfg.set("device", "name", value=body["name"])
        cfg.save()
        # 这些对象在构造时缓存了 device_id，统一换掉再重启相关组件
        speaker.device_id = did
        bridge.listener.device_id = did
        if hw:
            bridge.listener.hardware = hw
        history.action(f"切换设备 → {body.get('name') or did}")
        return {"ok": True, "device_id": did, "need_restart": True}

    # ---------- 自定义指令 ----------
    async def api_commands(_request):
        return web.json_response({
            "ok": True,
            "commands": router.store.list(),
            "action_types": [
                {"type": "play", "label": "播放（搜索词）",
                 "params": ["query", "mode", "limit"]},
                {"type": "play_folder", "label": "只播某个文件夹",
                 "params": ["path", "query", "mode", "limit"]},
                {"type": "play_track", "label": "播放固定曲目", "params": ["id"]},
                {"type": "pause", "label": "暂停"},
                {"type": "resume", "label": "继续"},
                {"type": "next", "label": "下一首"},
                {"type": "prev", "label": "上一首"},
                {"type": "stop", "label": "停止"},
                {"type": "volume", "label": "音量", "params": ["value", "delta"]},
                {"type": "mode", "label": "播放模式", "params": ["mode"]},
                {"type": "say", "label": "只说一句", "params": ["text"]},
                {"type": "status", "label": "报当前播放"},
                {"type": "rescan", "label": "重建曲库索引"},
                {"type": "openclaw", "label": "转给 OpenClaw（NAS 类问题）"},
            ],
        })

    @_need_json
    async def api_command_save(body):
        try:
            cid = body.get("id")
            if cid:
                cmd = router.store.update(cid, body)
                if not cmd:
                    return {"ok": False, "error": "指令不存在"}
            else:
                cmd = router.store.add(body)
            history.action(f"保存自定义指令「{cmd['name']}」")
            return {"ok": True, "command": cmd}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    @_need_json
    async def api_command_del(body):
        ok = router.store.delete(body.get("id", ""))
        if ok:
            history.action("删除自定义指令")
        return {"ok": ok, "error": "" if ok else "指令不存在"}

    @_need_json
    async def api_route_test(body):
        """WebUI 里的"试一句"：看它会被谁处理，可选真的执行"""
        text = (body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "先说一句话"}
        hit, intent = bridge.matcher.match(text)
        bare = False
        if not hit:
            # 跟真实链路一致：没叫名字时，看能不能靠自定义指令兜住
            plan = router.bare_plan(text)
            if not plan:
                return {"ok": True, "hit": False, "intent": intent,
                        "note": "没命中唤醒词，真实场景下不会处理"}
            bare = True
            intent = strip_device_wake(normalize(text)) or normalize(text)
        else:
            plan = await router.plan(
                intent,
                wake_scope=getattr(bridge.matcher, "last_scope", None),
                wake_name=getattr(bridge.matcher, "last_name", None))
        out = {"ok": True, "hit": True, "bare": bare, "intent": intent,
               "wake_name": getattr(bridge.matcher, "last_name", None),
               "wake_scope": getattr(bridge.matcher, "last_scope", None),
               "plan": plan.as_dict() if plan else None}
        if body.get("execute"):
            reply, ms = await bridge._run_plan(plan)
            out["executed"] = True
            out["reply"] = reply
            out["ms"] = ms
            history.action(f"测试指令「{text}」→ {plan.source}", detail=reply)
        return out

    # ---------- OpenClaw ----------
    def _oc_config_view():
        """读 openclaw.json 里的 mcp.servers（只读挂载，打码后展示）"""
        try:
            with open(OC_CONFIG, encoding="utf-8") as f:
                data = json.load(f)
            servers = (data.get("mcp") or {}).get("servers") or {}
            return {"path": OC_CONFIG, "ok": True,
                    "servers": {k: _mask(v) for k, v in servers.items()}}
        except FileNotFoundError:
            return {"path": OC_CONFIG, "ok": False,
                    "error": "未挂载 openclaw 配置（compose 里加 OC_CONFIG 挂载即可查看）"}
        except Exception as e:
            return {"path": OC_CONFIG, "ok": False, "error": str(e)}

    def _oc_log_tail(lines=40):
        try:
            with open(OC_LOG, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                text = f.read().decode("utf-8", "replace")
            return text.splitlines()[-lines:]
        except FileNotFoundError:
            return []
        except Exception as e:
            return [f"读取失败：{e}"]

    async def api_openclaw(_request):
        online = await agent.probe(ttl=15)
        return web.json_response({
            "ok": True,
            "agent": agent.status(),
            "reachable": online,
            "router": {
                "openclaw_mode": cfg.router.get("openclaw_mode", "fallback"),
                "openclaw_keywords": cfg.router.get("openclaw_keywords"),
                "stats": router.stats,
            },
            "openclaw_config": _oc_config_view(),
            "log_tail": _oc_log_tail(30),
            "snippet": {
                "transport": "stdio",
                "command": "/usr/bin/python3",
                "args": [MCP_HOST_SCRIPT],
                "env": {"XIAOAI_BRIDGE_URL": f"http://127.0.0.1:{cfg.server.get('port', 8790)}"},
            },
        })

    @_need_json
    async def api_openclaw_test(body):
        t0 = time.perf_counter()
        if not agent.configured:
            return {"ok": False, "error": "还没填网关地址"}
        try:
            # 跟随配置的超时（默认不限），不再写死 25s：
            # 网关冷启动时首响应可能 60s+，25s 会误报超时。
            reply = await agent.chat(body.get("text") or "只回两个字：通了")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        ms = int((time.perf_counter() - t0) * 1000)
        # 401/403 这类虽然连上了，但没跑通，别报"连通"
        ok = agent.online is True and not agent.last_error
        err = "" if ok else (agent.last_error or "网关不可达")
        if "401" in err:
            err = "网关拒绝访问（HTTP 401）：Token 不对或为空"
        elif "403" in err:
            err = "网关拒绝访问（HTTP 403）：需要 Token 或权限"
        return {"ok": ok, "ms": ms, "reply": reply, "error": err}

    @_need_json
    async def api_openclaw_config(body):
        c = cfg.raw.setdefault("openclaw", {})
        for key in ("base_url", "agent_id", "session_key"):
            if key in body:
                c[key] = str(body[key]).strip()
        if "model" in body:
            # 存的是"真实模型名"（如 agent2api/deepseek-v4.1-flash）：
            # 对话时 model 字段固定发 openclaw/<agentId>，真实模型走
            # x-openclaw-model 请求头（见网关文档 gateway/index.md）。
            import re as _re
            m = str(body["model"] or "").strip()
            if m:
                c["model"] = m if _re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9._-]+)*$", m) else "openclaw/main"
        if "timeout" in body:
            try:
                v = int(body["timeout"])
            except (TypeError, ValueError):
                v = 0
            # 0 = 不限制。网关有的查询要 30-60s，5s 上限必然被掐断。
            c["timeout"] = 0 if v <= 0 else max(30, min(600, v))
        if "enabled" in body:
            c["enabled"] = bool(body["enabled"])
        if body.get("token"):
            c["token"] = str(body["token"]).strip()      # 留空表示不改
        if "system_prefix" in body:
            c["system_prefix"] = body["system_prefix"]
        cfg._dirty = True
        cfg.save()
        agent.apply(cfg)
        agent._last_probe = 0
        history.action(f"更新 OpenClaw 配置：{c.get('base_url')}")
        return {"ok": True, "agent": agent.status()}

    # ---------- OpenClaw 自动识别 ----------
    async def api_openclaw_discover(_request):
        """探测本机可能跑着的 OpenClaw 网关，供前端一键填入"""
        host = oc_host_ip()
        ports = []
        try:                                    # 从挂载进来的 openclaw.json 里读真实端口
            with open(OC_CONFIG, encoding="utf-8") as f:
                p = (json.load(f).get("gateway") or {}).get("port")
            if p:
                ports.append(int(p))
        except Exception:
            pass
        for p in (48670, 48671, 18789):
            if p not in ports:
                ports.append(p)

        async def probe(urls):
            out = []
            for url in urls:
                ok, ms, err = False, None, ""
                try:
                    t0 = time.perf_counter()
                    # 容器里的 127.0.0.1 是自己，canonical 会换成 host.docker.internal
                    async with agent.session.get(oc_canonical(url) + "/health",
                                                 timeout=3) as r:
                        ok = r.status == 200
                    ms = int((time.perf_counter() - t0) * 1000)
                except Exception as e:
                    err = type(e).__name__
                out.append({"url": url, "ok": ok, "ms": ms, "error": err})
            return out

        # 网关在本机，优先按 127.0.0.1 展示；全都不通再退回宿主 IP 试一遍
        found = await probe([f"http://127.0.0.1:{p}" for p in ports])
        if not any(g["ok"] for g in found):
            found += await probe([f"http://{host}:{p}" for p in ports])

        # 已配置过的地址也带上，方便对照
        old = (cfg.openclaw.get("base_url") or "").rstrip("/")
        old = old.replace("//host.docker.internal", f"//{host}", 1)
        if old and old not in [g["url"] for g in found]:
            found += await probe([old])

        found.sort(key=lambda x: (not x["ok"], x["ms"] or 99999))
        return web.json_response({"ok": True, "gateways": found,
                                  "config_port": ports[0] if ports else None})

    async def autodiscover_openclaw(_app=None):
        """启动自检：网关地址为空时自己识别一次并写盘

        出厂配置从 1.2.1 起不带任何本机地址，所以新装、重装、清空配置之后
        openclaw.base_url 都是空字符串，界面与语音会一路报「未配置 base_url」。
        这里在服务起来后探一次本机常见端口（端口号也读 openclaw.json 里的
        gateway.port），探到就写入配置并立即生效，不必去 8790 页面点按钮。
        """
        if (cfg.openclaw.get("base_url") or "").strip():
            return
        await asyncio.sleep(2)                  # 等网关与端口转发就绪
        ports = []
        try:
            with open(OC_CONFIG, encoding="utf-8") as f:
                p = (json.load(f).get("gateway") or {}).get("port")
            if p:
                ports.append(int(p))
        except Exception:
            pass
        for p in (48670, 48671, 18789):
            if p not in ports:
                ports.append(p)
        for p in ports:
            url = f"http://127.0.0.1:{p}"
            try:
                async with agent.session.get(oc_canonical(url) + "/health",
                                             timeout=3) as r:
                    if r.status != 200:
                        continue
            except Exception:
                continue
            c = cfg.raw.setdefault("openclaw", {})
            c["base_url"] = url
            cfg._dirty = True
            cfg.save()
            agent.apply(cfg)
            agent._last_probe = 0
            log.info("OpenClaw 网关自动识别：%s（已写入配置）", url)
            return
        log.info("OpenClaw 网关未识别到，端口 %s 均不通", ports)

    async def api_openclaw_models(_request):
        """可选模型：只列网关真正接受的 id（openclaw / openclaw/<agent>）

        注意：后端真实模型（hs/…、agent2api/…）不能填在这里，
        网关会直接返回 400 Invalid `model`。这里把它作为提示单独返回。
        """
        cur = (cfg.openclaw.get("model") or "openclaw").strip() or "openclaw"
        models = []
        try:
            if agent.configured:
                async with agent.session.get(
                        f"{agent.base}/v1/models",
                        headers={"Authorization": f"Bearer {agent.token}"},
                        timeout=6) as r:
                    if r.status == 200:
                        models = [m.get("id") for m in (await r.json()).get("data", [])
                                  if m.get("id")]
        except Exception:
            pass

        backend = ""
        try:                                    # 给界面显示"当前后端模型"，只读
            with open(OC_CONFIG, encoding="utf-8") as f:
                d = ((json.load(f).get("agents") or {}).get("defaults") or {})
            m0 = d.get("model")
            if isinstance(m0, dict):
                m0 = m0.get("primary") or ""
            backend = m0 if isinstance(m0, str) else ""
        except Exception:
            pass

        if not models:
            models = ["openclaw"]
        if cur not in models:
            models.insert(0, cur)
        return web.json_response({"ok": True, "models": models, "current": cur,
                                  "backend_model": backend})

    async def api_openclaw_sessions(_request):
        """会话候选：当前配置 + 常用默认值（新增会话直接填新 key）"""
        cur = cfg.openclaw.get("session_key") or ""
        aid = cfg.openclaw.get("agent_id") or "main"
        keys = []
        for k in (cur, "xiaoai:livingroom", f"xiaoai:{aid}", "xiaoai:default"):
            if k and k not in keys:
                keys.append(k)
        return web.json_response({"ok": True, "sessions": keys, "current": cur})

    # ---------- 唤醒词 ----------
    def _wake_view():
        raw_names = cfg.wake.get("names")
        if raw_names is None:
            # 老配置只有正则：能还原的还原成名字，界面上就别再露正则了
            names = names_from_patterns(cfg.wake.get("patterns") or [])
            pats = []
            migrated = True
        else:
            names = [str(n) for n in raw_names if str(n).strip()]
            pats = [str(p) for p in (cfg.wake.get("patterns") or []) if str(p).strip()]
            migrated = False
        return {
            "ok": True,
            "names": names,
            "patterns": pats,
            "effective": list(bridge.matcher.names),
            "migrated": migrated,
            "bare_commands": bool(cfg.wake.get("bare_commands")),
            "sample": f"小爱同学，{(names or ['播放本地'])[0]}放首歌",
        }

    async def api_wake(_request):
        return web.json_response(_wake_view())

    @_need_json
    async def api_wake_save(body):
        names = [str(n).strip() for n in (body.get("names") or []) if str(n).strip()]
        pats = [str(p).strip() for p in (body.get("patterns") or []) if str(p).strip()]
        for p in pats:
            try:
                re.compile(p)
            except re.error as e:
                return {"ok": False, "error": f"正则写错了：{p}（{e}）"}
        if not names and not pats:
            return {"ok": False, "error": "至少留一个唤醒词，否则听不到你说话"}
        if len(names) > 8:
            return {"ok": False, "error": "名字最多 8 个，够用了"}
        for n in names:
            if len(n) > 8:
                return {"ok": False, "error": f"「{n}」太长了，名字最多 8 个字"}
        cfg.set("wake", "names", value=names)
        cfg.set("wake", "patterns", value=pats)
        if "bare_commands" in body:
            cfg.set("wake", "bare_commands", value=bool(body["bare_commands"]))
        cfg.save()
        bridge.matcher = WakeMatcher(cfg)      # 立刻生效，不用重启
        history.action("更新唤醒词", detail=" / ".join(names + pats))
        return _wake_view()

    @_need_json
    async def api_wake_test(body):
        text = (body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "先说一句话"}
        hit, intent = bridge.matcher.match(text)
        bare = False
        if not hit:
            # 没叫名字时，看看能不能靠自定义指令兜住（跟真实链路一致）
            p = router.bare_plan(text)
            if p:
                hit, bare = True, True
                intent = strip_device_wake(normalize(text)) or normalize(text)
        return {"ok": True, "hit": hit, "bare": bare, "intent": intent,
                "names": list(bridge.matcher.names),
                "bare_enabled": router.bare_enabled}

    @_need_json
    async def api_simulate(body):
        """把一句话当成音箱刚听到的内容，走完整链路（会播报、会真的放歌）

        用于不想对着音箱喊就能验证「唤醒词 → 路由 → 播放」整条链。
        """
        text = (body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "先说一句话"}
        asyncio.create_task(bridge.handle({"query": text}))
        history.action(f"模拟说一句「{text}」", detail="走完整链路")
        return {"ok": True, "text": text}

    # ---------- 外部触发：跑一条自定义指令（iOS 快捷指令 / 自动化） ----------
    # 名字形式的控制：手机快捷指令里只记「暂停播放」这种名字，不用记 action 参数
    CTRL_ALIAS = {
        "暂停播放": "pause", "暂停": "pause",
        "继续播放": "resume", "恢复播放": "resume",
        "下一首": "next", "上一首": "prev",
        "停止播放": "stop", "停止": "stop",
    }

    async def api_command_run(request):
        """POST {"id"|"name"|"text"|"action"} 或 GET ?id=/&name=/&text=/&action=

        固定指令用 id/name/text 定位；控制动作可直接给 action（pause/resume/next/prev/stop），
        也可照固定指令的写法给 name（?name=暂停播放），两者等价。
        不给参数时返回可用指令清单，方便在快捷指令里先查再跑。
        """
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                body = {}
        else:
            body = dict(request.query)

        cmds = [c for c in router.store.list() if c.get("enabled", True)]
        ident = str(body.get("id") or "").strip()
        name = str(body.get("name") or "").strip()
        text = str(body.get("text") or "").strip()
        action = str(body.get("action") or "").strip().lower()

        # name 写成「暂停播放」这类控制词时，等价于 action=pause；显式给 action 时以 action 为准
        if not action and name:
            action = CTRL_ALIAS.get(name, "")

        # 控制动作：直接调 player，跟 /api/pause、/api/next 等一致
        if action:
            ctl = {
                "pause": player.pause,
                "resume": player.resume,
                "next": player.next,
                "prev": player.prev,
                "stop": player.stop,
            }
            fn = ctl.get(action)
            if fn is None:
                return web.json_response(
                    {"ok": False, "error": f"不认识的 action：{action}",
                     "hint": "可选 pause/resume/next/prev/stop"}, status=400)
            r = await fn()
            history.action(f"快捷指令·{action}")
            return web.json_response({"ok": True, "action": action, **r})

        if not (ident or name or text):
            return web.json_response({"ok": True, "commands": [
                {"id": c["id"], "name": c.get("name"),
                 "patterns": c.get("patterns"), "type": (c.get("action") or {}).get("type")}
                for c in cmds]})

        cmd = None
        if ident:
            cmd = next((c for c in cmds if c["id"] == ident), None)
        if cmd is None and name:
            cmd = next((c for c in cmds if (c.get("name") or "") == name), None)
        if cmd is None and name:
            cmd = next((c for c in cmds if router._match(c, name)), None)
        if cmd is None and text:
            cmd = next((c for c in cmds if router._match(c, text)), None)
        if cmd is None:
            return web.json_response({"ok": False, "error": "没找到这条指令",
                                      "commands": [{"id": c["id"], "name": c.get("name")}
                                                   for c in cmds]}, status=404)

        intent = (cmd.get("patterns") or [cmd.get("name") or ""])[0]
        plan = router._custom(intent)
        if not plan:
            return web.json_response({"ok": False, "error": "这条指令没法执行"}, status=500)

        reply, ms = await bridge._run_plan(plan)
        history.action(f"外部触发指令「{cmd.get('name')}」", detail=reply)
        return web.json_response({"ok": True, "id": cmd["id"], "command": cmd.get("name"),
                                  "reply": reply, "ms": ms})

    # ---------- iOS 快捷指令配置 ----------
    async def api_shortcuts(request):
        """给控制台「快捷指令」模块生成可直接照抄的 iOS 快捷指令配置。

        每条固定指令 + 常用控制动作，返回：
        - siri: 快捷指令的名字（= 你对 Siri 说的话）
        - url: 请求地址（用 POST，静默执行、不弹网页）
        - body: JSON 请求体（键值对）
        - note: 一句话说明
        """
        # 对外地址：优先读配置 server.shortcuts_host，其次用浏览器访问的 host，
        # 最后用容器启动时注入的宿主局域网地址（XIAOAI_HOST_IP）。
        # 不用 localhost，否则生成的快捷指令手机连不上。
        host = (cfg.server.get("shortcuts_host") or "").strip()
        if not host:
            host = str(request.rel_url.host or "").strip()
        host = host or oc_host_ip()
        base = f"http://{host}:8790/api/command/run"

        items = []
        for c in router.store.list():
            if not c.get("enabled", True):
                continue
            # 与内置控制同名的固定指令不再单列，避免列表里出现两条一样的
            if (c.get("name") or "") in CTRL_ALIAS:
                continue
            items.append({
                "siri": c.get("name") or "播放",
                "url": base,
                # 用 name（指令名）直接填，比 id 好记。后端按名字精确匹配。
                "body": {"name": c.get("name")},
                "note": "执行固定指令「%s」" % c.get("name"),
            })

        controls = [
            ("暂停播放", "暂停当前播放"),
            ("继续播放", "继续播放"),
            ("下一首", "切到下一首"),
            ("上一首", "切到上一首"),
            ("停止播放", "停止播放"),
        ]
        for siri, note in controls:
            # 控制类也走 name，和固定指令一个写法，手机上不用记 action 参数
            items.append({
                "siri": siri, "url": base, "body": {"name": siri}, "note": note,
                "control": True,
            })

        return web.json_response({
            "ok": True, "base": base, "host": host,
            "steps": {
                "name": "为每条指令各建一条快捷指令",
                "actions": [
                    "添加操作：搜「获取 URL 的内容」",
                    "URL 填 base；方法选 POST；请求体选 JSON；按 body 填键值",
                    "把快捷指令名字改成 siri 那栏的值（这就是 Siri 的唤醒话）",
                ],
                "why_post": "用 GET 会打开网页；用 POST + JSON 才会静默后台执行，不弹窗。",
            },
            "items": items,
        })

    # ---------- 路由 / 指令元数据 ----------
    async def api_router(_request):
        return web.json_response({
            "ok": True,
            "openclaw_mode": cfg.router.get("openclaw_mode", "fallback"),
            "openclaw_keywords": cfg.router.get("openclaw_keywords"),
            "openclaw_wakes": cfg.router.get("openclaw_wakes") or [],
            "stats": router.stats,
        })

    @_need_json
    async def api_router_config(body):
        if "openclaw_mode" in body:
            mode = body["openclaw_mode"]
            if mode not in ("fallback", "keyword_only", "off"):
                return {"ok": False, "error": "模式只能是 fallback / keyword_only / off"}
            cfg.set("router", "openclaw_mode", value=mode)
        if "openclaw_keywords" in body:
            kws = [k.strip() for k in body["openclaw_keywords"] if k.strip()]
            cfg.set("router", "openclaw_keywords", value=kws)
        if "openclaw_wakes" in body:
            # OpenClaw 触发词（独立于本地唤醒词）：命中就整句转 agent
            wakes = [str(w).strip() for w in body["openclaw_wakes"] if str(w).strip()]
            if len(wakes) > 8:
                return {"ok": False, "error": "触发词最多 8 个"}
            for w in wakes:
                if len(w) > 12:
                    return {"ok": False, "error": f"「{w}」太长了，最多 12 个字"}
            cfg.set("router", "openclaw_wakes", value=wakes)
        cfg.save()
        bridge.matcher = WakeMatcher(cfg)      # 触发词参与匹配，立刻生效
        history.action("更新 OpenClaw 触发词")
        return {"ok": True, "router": {
            "openclaw_mode": cfg.router.get("openclaw_mode", "fallback"),
            "openclaw_keywords": cfg.router.get("openclaw_keywords"),
            "openclaw_wakes": cfg.router.get("openclaw_wakes") or [],
        }}

    # ---------- MCP 工具 ----------
    async def api_mcp(_request):
        try:
            import mcp_server
            tools = [{"name": t["name"], "description": t.get("description", "")}
                     for t in mcp_server.TOOLS]
        except Exception as e:
            tools = [{"name": "?", "description": f"读取失败：{e}"}]
        oc = _oc_config_view()
        linked = False
        if oc.get("ok"):
            linked = any("xiaoai" in k.lower() for k in oc["servers"])
        return web.json_response({
            "ok": True, "tools": tools, "bridge_url": f"http://127.0.0.1:{cfg.server.get('port', 8790)}",
            "openclaw": oc, "linked": linked,
            "snippet": {"mcpServers": {"xiaoai": {
                "transport": "stdio",
                "command": "/usr/bin/python3",
                "args": [MCP_HOST_SCRIPT],
                "env": {"XIAOAI_BRIDGE_URL": f"http://127.0.0.1:{cfg.server.get('port', 8790)}"},
            }}},
        })

    # ---------- 状态 ----------
    async def api_state(_request):
        st = await speaker.get_state(ttl=3.0)
        p = player.status()
        # 设备「离线」有两种完全不同的原因，界面必须分开显示：
        #   ① 登录态失效（passToken 过期）→ 我们根本发不出指令，扫码就能好
        #   ② 音箱真的断网/断电        → 只能等设备恢复
        # 只看 online=false 会把 ① 误报成 ②，让人去查音箱电源。
        acc = auth.status()
        # 注意：status() 的 needs_login 只看「本地还有没有 token 文件」，
        # 失效的旧 token 也会让它报 False。真正判据是 last_auth_error
        # （passToken 失效 / 续期被拒时会被写入）。
        auth_err = (getattr(auth, "last_auth_error", "") or acc.get("auth_error") or "").strip()
        return web.json_response({
            "ok": True,
            "auth": {"needs_login": bool(acc.get("needs_login") or auth_err),
                     "error": auth_err},
            "device": {
                "name": cfg.device.get("name", "小爱音箱"),
                "id": cfg.device.get("device_id", ""),
                "hardware": cfg.device.get("hardware", ""),
                "online": st.get("online", False),
                "status": st.get("status", -1),
                "volume": st.get("volume"),
            },
            "player": p,
            "speak": _speak_view(),
            "library": {
                "count": len(library.tracks),
                "roots": library.roots,
                "roots_info": library.roots_info(),
                "genre_covered": sum(1 for t in library.tracks if t.genre),
                "genres": [{"name": k, "count": v}
                           for k, v in library.genres()[:12]],
                "scanned_at": library.loaded_at,
            },
            "account": {
                "logged_in": auth.has_service_token,
                "user_id": auth.user_id,
            },
            "openclaw": {
                "configured": agent.configured,
                "enabled": agent.enabled,
                "online": agent.online,
                "base_url": agent.base_raw,
                "last_ms": agent.last_ms,
            },
            "engine": {
                "session": cfg.openclaw.get("session_key") or "",
                "poll_sec": cfg.listen.get("poll_sec", 0.5),
                "public_base": speaker.public_base,
                "uptime": int(time.time() - STARTED_AT),
                "mode": player.mode,
            },
            "router": router.stats,
            "stats": history.stats(),
        })

    async def api_status(_request):
        return web.json_response(player.status())

    async def api_queue(_request):
        return web.json_response({"ok": True, "queue": player.queue_list(),
                                  "cursor": player.cursor,
                                  "mode": player.mode})

    async def api_history(request):
        limit = int(request.query.get("limit", 40))
        return web.json_response({"ok": True,
                                  "items": history.list(limit=limit)})

    async def api_memory(request):
        """记忆面板：看它到底记住了什么、会喂给模型什么"""
        m = getattr(bridge, "memory", None)
        if not m:
            return web.json_response({"ok": False, "error": "记忆未启用"})
        try:
            recent = [{"name": n, "artist": a, "action": act}
                      for n, a, act in m.recent_plays(12)]
            turns = [{"query": q, "reply": r} for q, r in m.recent_turns(5)]
            liked = [{"artist": k, "score": round(v, 1)}
                     for k, v, _ in m._decayed("artist", 1, 8)]
            disliked = [{"artist": k, "score": round(v, 1), "hits": h}
                        for k, v, h in m._decayed("artist", -1, 5)]
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)[:120]})
        return web.json_response({"ok": True, "stats": m.stats(),
                                  "context": m.context(), "recent": recent,
                                  "turns": turns, "liked": liked,
                                  "disliked": disliked})

    async def api_config(_request):
        """只暴露安全的展示字段，token/密码一律不出网关"""
        c = json.loads(json.dumps(cfg.raw))
        mi = dict(c.get("mi") or {})
        mi["has_password"] = bool(mi.get("password"))
        mi.pop("password", None)
        oc = dict(c.get("openclaw") or {})
        oc["has_token"] = bool(oc.get("token"))
        oc.pop("token", None)
        c["mi"], c["openclaw"] = mi, oc
        return web.json_response({"ok": True, "config": c})

    async def index(_request):
        path = os.path.join(web_dir, "index.html")
        if not os.path.isfile(path):
            return web.Response(text="WebUI 未安装", content_type="text/plain")
        return web.FileResponse(path)

    async def api_backend_models(_request):
        """后端真实模型清单（openclaw.json 的 models.providers）

        网关只认 openclaw / openclaw/<agent>；真正换模型是改
        agents.defaults.model.primary，这里列出可选值。
        """
        try:
            with open(OC_CONFIG, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            return web.json_response({"ok": False, "error": "读不到 openclaw.json：%s" % e})

        providers = ((d.get("models") or {}).get("providers") or {})
        options = []
        for pname, pv in providers.items():
            for m in (pv.get("models") or []):
                mid = str(m.get("id") or "").strip()
                if not mid:
                    continue
                options.append({"value": "%s/%s" % (pname, mid),
                                "provider": pname, "model": mid,
                                "label": "%s · %s" % (pname, mid)})

        m0 = ((d.get("agents") or {}).get("defaults") or {}).get("model")
        if isinstance(m0, dict):
            m0 = m0.get("primary") or ""
        return web.json_response({"ok": True, "current": m0 or "",
                                  "options": options, "path": OC_CONFIG})

    @_need_json
    async def api_backend_model_set(body):
        want = str(body.get("model") or "").strip()
        if not want:
            return {"ok": False, "error": "缺少 model"}
        try:
            with open(OC_CONFIG, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            return {"ok": False, "error": "读不到 openclaw.json：%s" % e}

        providers = ((d.get("models") or {}).get("providers") or {})
        allowed = {"%s/%s" % (p, m.get("id")) for p, v in providers.items()
                   for m in (v.get("models") or [])}
        # 配置里的大小写不一定和 provider key 一致（如 agent2api vs Agent2API）
        by_lower = {k.lower(): k for k in allowed}
        want = by_lower.get(want.lower(), want)
        if want not in allowed:
            return {"ok": False, "error": "不认识的模型：%s" % want}

        node = d.setdefault("agents", {}).setdefault("defaults", {})
        cur = node.get("model")
        if isinstance(cur, dict):
            cur["primary"] = want
        else:
            node["model"] = {"primary": want}

        try:
            st = os.stat(OC_CONFIG)
            with open(OC_CONFIG, encoding="utf-8") as f:
                src = f.read()
            # 备份放到自己的 conf 里（openclaw 的目录别塞杂物）
            bak_dir = "/app/conf"
            if not os.path.isdir(bak_dir):
                bak_dir = os.path.dirname(OC_CONFIG)
            try:
                with open(os.path.join(bak_dir, "openclaw.json.bak-model-"
                                       + time.strftime("%Y%m%d-%H%M%S")),
                          "w", encoding="utf-8") as f:
                    f.write(src)
            except Exception:
                pass
            # 原地写（保留 inode，网关 watch 才会触发热加载）
            with open(OC_CONFIG, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            try:
                os.chown(OC_CONFIG, st.st_uid, st.st_gid)
            except Exception:
                pass
        except OSError as e:
            if getattr(e, "errno", None) == 30:
                return {"ok": False,
                        "error": "写入失败：%s 是只读挂载，无法切换" % OC_CONFIG}
            return {"ok": False, "error": "写入失败：%s" % e}
        except Exception as e:
            return {"ok": False, "error": "写入失败：%s" % e}

        log.info("后端模型切换为 %s", want)
        return {"ok": True, "model": want, "note": "网关会自动热加载，几秒内生效"}

    app.router.add_post("/api/auth/login", security.api_login)
    app.router.add_get("/media/{name}", media)
    app.router.add_get("/audio/{name}", audio)
    app.router.add_get("/m/{name}", short_audio)
    app.router.add_get("/health", lambda r: web.json_response(
        {"ok": True, "uptime": int(time.time() - STARTED_AT)}))
    app.router.add_post("/api/say", api_say)
    app.router.add_post("/api/play_local", api_play_local)
    app.router.add_post("/api/play_id", api_play_id)
    app.router.add_post("/api/next", api_next)
    app.router.add_post("/api/prev", api_prev)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/pause", api_pause)
    app.router.add_post("/api/resume", api_resume)
    app.router.add_post("/api/jump", api_jump)
    app.router.add_post("/api/mode", api_mode)
    app.router.add_post("/api/volume", api_volume)
    app.router.add_get("/api/speak", api_speak_get)
    app.router.add_post("/api/speak", api_speak_save)
    app.router.add_post("/api/search", api_search)
    app.router.add_post("/api/library/rescan", api_rescan)
    app.router.add_get("/api/library/roots", api_roots)
    app.router.add_post("/api/library/roots", api_roots_add)
    app.router.add_post("/api/library/roots/remove", api_roots_del)
    app.router.add_get("/api/mi", api_mi)
    app.router.add_post("/api/mi/qr", api_mi_qr)
    app.router.add_get("/api/mi/qr", api_mi_qr_poll)
    app.router.add_post("/api/mi/refresh", api_mi_refresh)
    app.router.add_post("/api/mi/password", api_mi_password)
    app.router.add_post("/api/mi/devices", api_mi_devices)
    app.router.add_post("/api/mi/device", api_mi_device)
    app.router.add_get("/api/commands", api_commands)
    app.router.add_get("/api/wake", api_wake)
    app.router.add_post("/api/wake", api_wake_save)
    app.router.add_post("/api/wake/test", api_wake_test)
    app.router.add_post("/api/simulate", api_simulate)
    app.router.add_post("/api/commands", api_command_save)
    app.router.add_post("/api/commands/delete", api_command_del)
    app.router.add_post("/api/route/test", api_route_test)
    app.router.add_post("/api/command/run", api_command_run)
    app.router.add_get("/api/command/run", api_command_run)
    app.router.add_get("/api/shortcuts", api_shortcuts)
    app.router.add_get("/api/router", api_router)
    app.router.add_post("/api/router", api_router_config)
    app.router.add_get("/api/openclaw", api_openclaw)
    app.router.add_post("/api/openclaw/test", api_openclaw_test)
    app.router.add_post("/api/openclaw/config", api_openclaw_config)
    app.router.add_get("/api/openclaw/discover", api_openclaw_discover)
    app.on_startup.append(autodiscover_openclaw)
    app.router.add_get("/api/openclaw/models", api_openclaw_models)
    app.router.add_get("/api/openclaw/backend-models", api_backend_models)
    app.router.add_post("/api/openclaw/backend-model", api_backend_model_set)
    app.router.add_get("/api/openclaw/sessions", api_openclaw_sessions)
    app.router.add_get("/api/tags", api_tags)
    app.router.add_post("/api/tags/build", api_tags_build)
    app.router.add_get("/api/tags/one", api_tags_one)
    app.router.add_get("/api/donate", api_donate)
    app.router.add_get("/api/donate/qr", api_donate_qr)
    app.router.add_get("/api/llm", api_llm_get)
    app.router.add_post("/api/llm/config", api_llm_save)
    app.router.add_post("/api/llm/test", api_llm_test)
    app.router.add_get("/api/mcp", api_mcp)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/queue", api_queue)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/memory", api_memory)
    app.router.add_get("/api/artists", api_artists)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/", index)
    app.router.add_static("/ui/", web_dir, show_index=False)
    log.info("WebUI 挂载于 / （目录 %s）", web_dir)
