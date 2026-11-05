"""本地音乐库：扫描、检索、定位

agent 不要自己 find（NAS 上 /vol1/1000 对其他用户不可列举），
统一通过这里检索，路径与权限都由 bridge 负责。
"""
import hashlib
import json
import logging
import os
import random
import re
import time

log = logging.getLogger("music")

DEFAULT_EXTS = [".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg", ".wma"]

# 索引结构有改动时 +1，旧缓存自动失效
CACHE_VERSION = 3

# 目录/文件名里的格式噪音：「周杰伦无损fla」「320k」「全集」这类
NOISE = re.compile(
    r"(无损|高音质|正版|flac|fla|mp3|ape|wav|wma|320k|320|hq|cd\d{1,2}|"
    r"专辑|全集|合集|歌单|歌曲|单曲)", re.I)
# 文件名开头的曲序号：「01. 」「03 - 」
TRACKNUM = re.compile(r"^\s*\d{1,3}\s*[\.\-_、 ]\s*")
# 纯数字/日期的"歌手"其实不是歌手
JUNK_ARTIST = re.compile(r"^[\d\s\.\-_年月日]+$")


def clean_text(s):
    s = NOISE.sub("", s or "")
    s = re.sub(r"[\s\-_·\.]+$", "", s).strip()
    return s.strip()


def clean_album(s):
    """去掉「22-2018-」「03 - 」这类曲序/年份前缀"""
    s = s or ""
    for _ in range(3):
        new = re.sub(r"^\s*(?:\d{1,3}|(?:19|20)\d{2})\s*[-\._ ]\s*", "", s)
        if new == s:
            break
        s = new
    return clean_text(s)


def strip_artist_prefix(name, artist):
    """「周杰伦-不爱我就拉倒」→「不爱我就拉倒」（文件名常用不带空格的连字符）"""
    if not artist or not name:
        return name
    out = re.sub(rf"^\s*{re.escape(artist)}\s*[-—_]+\s*", "", name).strip()
    return out or name


def looks_like_artist(s):
    """文件名「A - B」里的 A 是否像个歌手名（而不是曲序号）"""
    if not s or len(s) < 1:
        return False
    if JUNK_ARTIST.match(s):
        return False
    return True


class Track:
    __slots__ = ("path", "name", "artist", "album", "tid")

    def __init__(self, path, name, artist, album):
        self.path = path
        self.name = name
        self.artist = artist
        self.album = album
        self.tid = hashlib.md5(path.encode("utf-8")).hexdigest()[:12]

    def as_dict(self):
        return {"id": self.tid, "name": self.name, "artist": self.artist,
                "album": self.album}

    def __str__(self):
        if self.artist:
            return f"{self.artist} - {self.name}"
        return self.name


class MusicLibrary:
    def __init__(self, cfg):
        self.cfg = cfg
        m = cfg.music or {}
        self.roots = [cfg.abspath(r) for r in (m.get("roots") or [])]
        self.exts = tuple(x.lower() for x in (m.get("exts") or DEFAULT_EXTS))
        self.cache_path = cfg.abspath(m.get("index_cache") or "./conf/music_index.json")
        self.tracks = []
        self.by_id = {}
        self.loaded_at = 0

    # ---------- 扫描 ----------
    def _collect(self, root, seen):
        """扫一个根目录，返回 Track 列表；seen 用于跨目录去重"""
        found = []
        if not os.path.isdir(root):
            log.warning("音乐根目录不存在：%s", root)
            return found
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if not d.startswith(("@", "."))]
            rel = os.path.relpath(dirpath, root)
            parts = [] if rel == "." else rel.split(os.sep)
            album = clean_album(parts[-1]) if parts else ""
            folder_artist = clean_text(parts[0]) if len(parts) >= 2 else ""
            for fn in filenames:
                if not fn.lower().endswith(self.exts):
                    continue
                stem = os.path.splitext(fn)[0]
                track_name = TRACKNUM.sub("", stem)
                # 文件名里的「A - B」到底谁是谁：
                #   上一层目录名（缺省用当前目录）如果是 B，说明文件被写成
                #   「歌名 - 歌手」，直接采信 B 是歌手；否则按常规「歌手 - 歌名」。
                #   这样 三巨头早年歌曲/汪苏泷/万有引力/万有引力 - 汪苏泷.flac
                #   才不会把歌名当成歌手。
                file_artist = ""
                if " - " in track_name:
                    left, right = track_name.split(" - ", 1)
                    left, right = left.strip(), right.strip()
                    parent = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else "")
                    parent = clean_text(parent)
                    if parent and right and clean_text(right) == parent \
                            and clean_text(left) != parent:
                        file_artist, track_name = right, left
                    elif right and looks_like_artist(left):
                        file_artist, track_name = left, right
                artist = clean_text(file_artist) or folder_artist
                track_name = strip_artist_prefix(
                    TRACKNUM.sub("", track_name).strip() or stem, artist)
                # 同一首歌在多个目录有副本，按「歌手+歌名」去重
                key = (artist.lower(), track_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                found.append(Track(os.path.join(dirpath, fn), track_name,
                                   artist, album))
        return found

    def scan(self, force=False):
        if not self.roots:
            # 目录全删了要把索引也清掉，否则旧歌还留在列表里能播
            if self.tracks:
                log.info("音乐目录已清空，重置音乐库")
            self.tracks = []
            self.by_id = {}
            self.loaded_at = int(time.time())
            self._save_cache()
            return 0
        if not force and self.tracks:
            return len(self.tracks)
        if not force and self._load_cache():
            return len(self.tracks)

        t0 = time.time()
        found = []
        seen = set()
        for root in self.roots:
            found.extend(self._collect(root, seen))
        self.tracks = found
        self.by_id = {t.tid: t for t in found}
        self.loaded_at = int(t0)
        log.info("音乐库索引完成：%d 首，用时 %.1fs", len(found), time.time() - t0)
        self._save_cache()
        return len(found)

    # ---------- 根目录管理 ----------
    def roots_info(self):
        """WebUI 展示用：每个根是否可见、里面有多少首歌"""
        out = []
        for r in self.roots:
            exists = os.path.isdir(r)
            out.append({
                "path": r,
                "exists": exists,
                "visible": exists,
                "readable": exists and os.access(r, os.R_OK),
                "count": sum(1 for t in self.tracks if t.path.startswith(r + os.sep)),
            })
        return out

    def add_root(self, path):
        """新增音乐目录：先验证可见性，再写回 config.json"""
        path = (path or "").strip().rstrip("/")
        if not path:
            return {"ok": False, "error": "路径为空"}
        if not os.path.isabs(path):
            return {"ok": False, "error": "请填绝对路径，例如 /vol1/1000/音乐文件"}
        if path in self.roots:
            return {"ok": False, "error": "这个路径已经加过了"}
        if not os.path.isdir(path):
            return {"ok": False,
                    "error": "找不到这个目录，请填 NAS 上真实存在的绝对路径",
                    "need_mount": True}
        if not os.access(path, os.R_OK):
            return {"ok": False,
                    "error": "目录存在但没有读权限（NAS 上用 ACL 授权给运行用户）"}
        self.roots.append(path)
        self.cfg.set("music", "roots", value=list(self.roots))
        self.cfg.save()
        log.info("新增音乐目录 %s", path)
        return {"ok": True, "roots": self.roots}

    def remove_root(self, path):
        path = (path or "").strip().rstrip("/")
        if path not in self.roots:
            return {"ok": False, "error": "路径不在列表里"}
        self.roots = [r for r in self.roots if r != path]
        self.cfg.set("music", "roots", value=list(self.roots))
        self.cfg.save()
        return {"ok": True, "roots": self.roots}


    def _load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != CACHE_VERSION:
                log.info("索引缓存版本不符，重新扫描")
                return False
            if set(data.get("roots") or []) != set(self.roots):
                return False
            self.tracks = [Track(d["path"], d["name"], d["artist"], d["album"])
                           for d in data.get("tracks", [])]
            self.by_id = {t.tid: t for t in self.tracks}
            log.info("音乐库从缓存加载：%d 首", len(self.tracks))
            return bool(self.tracks)
        except Exception:
            return False

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"version": CACHE_VERSION,
                           "roots": self.roots,
                           "tracks": [{"path": t.path, "name": t.name,
                                       "artist": t.artist, "album": t.album}
                                      for t in self.tracks]},
                          f, ensure_ascii=False)
        except Exception as e:
            log.warning("音乐索引落盘失败：%s", e)

    # ---------- 检索 ----------
    def search(self, query, limit=60):
        """limit<=0 表示不截断（要整个文件夹/整个曲库时用）"""
        q = (query or "").strip().lower()
        if not q:
            return []
        self.scan()
        scored = []
        for t in self.tracks:
            hay = f"{t.artist} {t.name} {t.album} {os.path.basename(t.path)}".lower()
            if q in hay:
                score = 0
                if q in t.name.lower():
                    score += 3
                if q in t.artist.lower():
                    score += 2
                if hay.startswith(q):
                    score += 1
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            # 拆词再试一次（"周杰伦 晴天" -> 分别匹配）
            words = [w for w in q.replace("　", " ").split() if w]
            if len(words) > 1:
                scored = [(0, t) for t in self.tracks
                          if all(w in f"{t.artist} {t.name}".lower() for w in words)]
        if limit and limit > 0:
            scored = scored[:limit]
        return [t for _, t in scored]

    def pick(self, query, mode="shuffle", limit=30):
        """按查询词取歌。先取全部命中，随机模式下打散，最后才按上限截断——
        否则"随机播放"只是在文件名排序的前 N 首里随机，听起来不像随机。"""
        tracks = self.search(query, limit=0)
        if mode == "shuffle" and len(tracks) > 1:
            random.shuffle(tracks)
        if limit and limit > 0:
            tracks = tracks[:limit]
        return tracks

    def get(self, tid):
        return self.by_id.get(tid)

    def search_in(self, path, query="", limit=50, shuffle=False):
        """只在某个目录里找（自定义指令「只放回忆文件夹」用）

        目录未必在 music.roots 之下，所以索引里找不到就现扫一遍。
        limit<=0 = 整个文件夹，不截断；shuffle=True 先把文件夹里的歌打散再截断，
        这样随机播放才覆盖到整个文件夹，而不是前 N 首。
        """
        base = os.path.realpath(path) if path else ""
        if not base or not os.path.isdir(base):
            log.warning("search_in: 目录不可见 %s", path)
            return []
        prefix = base + os.sep
        pool = [t for t in self.tracks if t.path.startswith(prefix)]
        if not pool:
            pool = self._collect(base, set())
            if pool:
                log.info("目录未在索引内，临时扫描 %s 得到 %d 首", base, len(pool))
        q = (query or "").strip().lower()
        if q:
            pool = [t for t in pool
                    if q in f"{t.artist} {t.name} {t.album}".lower()]
        if shuffle and len(pool) > 1:
            random.shuffle(pool)
        if limit and limit > 0:
            pool = pool[:limit]
        return pool

    def random_pick(self, n=20):
        """n<=0 = 全库打散后全给（不截断）"""
        self.scan()
        if not self.tracks:
            return []
        pool = list(self.tracks)
        random.shuffle(pool)
        if n and n > 0:
            pool = pool[:n]
        return pool
