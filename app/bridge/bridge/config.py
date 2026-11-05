"""配置加载与写回

WebUI 要能改音乐路径、自定义指令、OpenClaw 地址，所以配置不再只读。
写回用「临时文件 + 原子替换」，避免写一半断电留下坏 JSON。
"""
import json
import logging
import os
import tempfile

log = logging.getLogger("config")

# 单文件挂载只能原地写，日志提示一次就够，别每次改配置都刷
_busy_noted = False


class Config:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        with open(self.path, encoding="utf-8") as f:
            self.raw = json.load(f)
        base = os.path.dirname(self.path)
        # 相对路径统一按配置文件所在目录解析
        self._base = base
        self._dirty = False

    # ---------- 读 ----------
    def get(self, *keys, default=None):
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def abspath(self, p):
        if not p:
            return p
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self._base, p))

    # ---------- 写 ----------
    def set(self, *keys, value):
        """设置某个键（自动补中间层），返回旧值"""
        if not keys:
            raise ValueError("缺少键")
        cur = self.raw
        for k in keys[:-1]:
            nxt = cur.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[k] = nxt
            cur = nxt
        old = cur.get(keys[-1])
        if old != value:
            cur[keys[-1]] = value
            self._dirty = True
        return old

    def save(self):
        """原子写回。config 里有 token，写完后收紧权限。"""
        if not self._dirty:
            return False
        d = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(prefix=".config.", dir=d, text=True)
        # 容器里是 root，写回后要保持宿主文件原有的属主/权限，否则 NAS 上就打不开了
        try:
            st = os.stat(self.path)
        except OSError:
            st = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.raw, f, ensure_ascii=False, indent=2)
                f.write("\n")
            if st is not None:
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except OSError:
                    pass
            try:
                os.replace(tmp, self.path)
            except OSError as e:
                # 单文件 bind mount（docker 挂 config.json）不允许 rename，
                # 报 EBUSY。退回原地覆写：牺牲原子性，换取能改配置。
                if e.errno not in (16, 18):          # EBUSY / EXDEV
                    raise
                global _busy_noted
                if not _busy_noted:
                    _busy_noted = True
                    log.info("配置是挂载点，改为原地写回（%s）；后续不再重复提示", e.strerror)
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.raw, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            try:
                os.chmod(self.path, (st.st_mode & 0o777) if st else 0o600)
            except OSError:
                pass
            self._dirty = False
            log.info("配置已写回 %s", self.path)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- 分组 ----------
    @property
    def mi(self):
        return self.raw.get("mi", {})

    @property
    def device(self):
        return self.raw.get("device", {})

    @property
    def listen(self):
        return self.raw.get("listen", {})

    @property
    def wake(self):
        return self.raw.get("wake", {})

    @property
    def openclaw(self):
        return self.raw.get("openclaw", {})

    @property
    def router(self):
        return self.raw.get("router", {})

    @property
    def music(self):
        return self.raw.get("music", {})

    @property
    def speak(self):
        return self.raw.get("speak", {})

    @property
    def server(self):
        return self.raw.get("server", {})
