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
CACHE_VERSION = 6

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


# 下载站常见的文件名：曲名-歌手-歌曲ID-码率，例如
#   不是因为寂寞才想你-陶钰玉-464207-2000
#   Stronger_(DJ版)-Inez-354539010-100
# 末尾两段都必须是纯数字才算，避免把「2002年的第一场雪」这类正常名字切错。
DL_NAME = re.compile(r"^(.+?)-([^-]{1,30}?)-(\d{3,})-(\d{2,4})$")


def split_download_name(stem):
    """从「曲名-歌手-编号-码率」里拆出 (曲名, 歌手)；不像的直接返回空"""
    m = DL_NAME.match((stem or "").strip())
    if not m:
        return "", ""
    title, artist = m.group(1).strip(" -_"), m.group(2).strip(" -_")
    if not title or not artist:
        return "", ""
    if JUNK_ARTIST.match(artist) or artist.lower() in ("unknown", "va", "various"):
        return "", ""
    if any(ch.isdigit() for ch in artist) and len(artist) <= 2:
        return "", ""
    return clean_text(title), clean_text(artist)


def looks_like_artist(s):
    """文件名「A - B」里的 A 是否像个歌手名（而不是曲序号）"""
    if not s or len(s) < 1:
        return False
    if JUNK_ARTIST.match(s):
        return False
    return True


# 流派归一到几大类：刮削工具写进来的写法五花八门（国语流行/pop/j-pop），
# 不合并的话检索时对不上。这里只做「同义合并」和「丢弃垃圾值」，
# 不做语义扩展——那交给模型判断。
GENRE_ALIAS = {
    "pop": "流行", "国语流行": "流行", "国际流行": "流行", "粤语流行": "流行",
    "华语流行": "流行", "中文流行": "流行", "j-pop": "流行", "k-pop": "流行",
    "c-pop": "流行", "流行音乐": "流行",
    "folk": "民谣", "民谣": "民谣",
    "电子": "电子", "电子音乐": "电子", "舞曲": "电子", "electronic": "电子",
    "edm": "电子", "trance": "电子", "house": "电子", "progressive house": "电子",
    "techno": "电子", "dubstep": "电子", "dance": "电子",
    "纯音乐": "纯音乐", "轻音乐": "纯音乐", "钢琴": "纯音乐", "piano": "纯音乐",
    "古典": "纯音乐", "classical": "纯音乐", "classical crossover": "纯音乐",
    "soundtrack": "纯音乐", "原声": "纯音乐",
    "摇滚": "摇滚", "rock": "摇滚",
    "说唱": "说唱", "rap": "说唱", "嘻哈": "说唱", "hiphop": "说唱",
    "网络歌曲": "网络歌曲",
    # 已知错标 / 下载站垃圾值：当没有流派处理
    "blues": "", "kuwo": "", "wusunk.com": "", "wusunk.com分享": "",
    "other": "", "<unknown>": "", "unknown": "", "misc": "",
}

_TAG_KEYS = {
    "title": ("title", "TIT2", "©nam"),
    "artist": ("artist", "TPE1", "©ART", "albumartist"),
    "album": ("album", "TALB", "©alb"),
    "genre": ("genre", "TCON", "©gen"),
}


def _tag_text(tags, keys):
    for k in keys:
        # FLAC/Vorbis 的标签是 VCFLACDict，`k in tags` 对非 ASCII 键
        # （©nam / ©alb 这类 MP4 键）会直接抛 ValueError。老代码没兜住，
        # 一个字段缺少就整个文件的标签全丢，artist/album 都退回文件名。
        try:
            hit = k in tags
        except Exception:
            continue
        if hit:
            v = tags[k]
            if isinstance(v, list):
                v = v[0] if v else ""
            v = str(v).strip()
            if v:
                return v
    return ""


def read_tags(path):
    """读音频标签，失败返回空 dict（不装 ffmpeg，只用 mutagen）"""
    out = {}
    try:
        from mutagen import File as _File
        t = _File(path)
        tags = (t.tags or {}) if t else {}
        if not tags:
            return out
        for field, keys in _TAG_KEYS.items():
            out[field] = _tag_text(tags, keys)
    except Exception:
        return {}
    return out


def normalize_genre(raw):
    """把流派值归一到大类；未知值原样保留（便于发现新的流派）"""
    g = (raw or "").strip().lower()
    if not g:
        return ""
    if g in GENRE_ALIAS:
        return GENRE_ALIAS[g]
    # 多值（Pop;Rock）取第一个能认出来的
    for part in re.split(r"[;/,、]", g):
        part = part.strip()
        if part and part in GENRE_ALIAS:
            return GENRE_ALIAS[part]
    return (raw or "").strip()


class Track:
    __slots__ = ("path", "name", "artist", "album", "genre", "tid", "mtime")

    def __init__(self, path, name, artist, album, genre="", mtime=0.0):
        self.path = path
        self.name = name
        self.artist = artist
        self.album = album
        self.genre = genre
        self.mtime = float(mtime or 0)
        self.tid = hashlib.md5(path.encode("utf-8")).hexdigest()[:12]

    def as_dict(self):
        return {"id": self.tid, "name": self.name, "artist": self.artist,
                "album": self.album, "genre": self.genre}

    def __str__(self):
        if self.artist:
            return f"{self.artist} - {self.name}"
        return self.name

    @property
    def pretty(self):
        """给播报用的短名字：去掉下划线、结尾的编号与码率后缀

        库里不少文件名是「解药-颜小健_郑国锋-51573022-320」这种带下载编号的，
        原样念出来又长又不像话，还会把播报拖长好几秒。
        """
        nm = self.name
        nm = re.sub(r"\.(mp3|flac|m4a|wav|ape|ogg|wma)$", "", nm, flags=re.I)
        nm = nm.replace("_", " ").strip()
        for _ in range(3):
            before = nm
            nm = re.sub(r"-\d{4,}$", "", nm).strip()
            nm = re.sub(r"-\d{3}$", "", nm).strip()
            nm = re.sub(r"-\d{2,3}k(bps)?$", "", nm, flags=re.I).strip()
            if nm == before:
                break
        nm = re.sub(r"\s{2,}", " ", nm).strip(" -")
        if not nm:
            nm = self.name
        return f"{self.artist} - {nm}" if self.artist else nm


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
        self._reused = 0

    # ---------- 扫描 ----------
    def _collect(self, root, seen, old=None):
        """扫一个根目录，返回 Track 列表；seen 用于跨目录去重

        old 是「上次索引」的 path → Track 映射。路径相同且 mtime 没变的
        直接复用旧记录、不再读标签 —— 1900 多首里通常只有几首是新的，
        这样重建索引是秒级，而不是每次两分钟。
        """
        found = []
        if not os.path.isdir(root):
            log.warning("音乐根目录不存在：%s", root)
            return found
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if not d.startswith(("@", "."))]
            rel = os.path.relpath(dirpath, root)
            parts = [] if rel == "." else rel.split(os.sep)
            # 目录推导出来的专辑只做兜底。注意别在循环里复用同名变量：
            # 否则上一首的 album 会被没标签的文件继承，整目录串成一个专辑。
            folder_album = clean_album(parts[-1]) if parts else ""
            folder_artist = clean_text(parts[0]) if len(parts) >= 2 else ""
            for fn in filenames:
                if not fn.lower().endswith(self.exts):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    mt = os.path.getmtime(full)
                except OSError:
                    mt = 0.0
                if old:
                    prev = old.get(full)
                    if prev is not None and prev.mtime \
                            and abs(prev.mtime - mt) < 1e-6:
                        key = (prev.artist.lower(), prev.name.lower())
                        if key in seen:
                            continue
                        seen.add(key)
                        found.append(prev)
                        self._reused += 1
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
                if not file_artist:
                    # 「曲名-歌手-编号-码率」这种下载站命名：整串被当成了曲名
                    _title, _artist = split_download_name(stem)
                    if _artist:
                        track_name, file_artist = _title, _artist
                # 标签优先，路径兜底：刮削过的文件以 tag 为准，
                # 没 tag 的（大量下载的 flac）继续用目录/文件名推断
                tags = read_tags(os.path.join(dirpath, fn))
                artist = (clean_text(tags.get("artist", ""))
                          or clean_text(file_artist) or folder_artist)
                tag_title = clean_text(tags.get("title", ""))
                if tag_title:
                    track_name = strip_artist_prefix(tag_title, artist)
                else:
                    track_name = strip_artist_prefix(
                        TRACKNUM.sub("", track_name).strip() or stem, artist)
                album = clean_album(tags.get("album", "")) or folder_album
                genre = normalize_genre(tags.get("genre", ""))
                # 同一首歌在多个目录有副本，按「歌手+歌名」去重
                key = (artist.lower(), track_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                found.append(Track(full, track_name,
                                   artist, album, genre, mt))
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
        self._reused = 0
        found = []
        seen = set()
        # 旧索引按路径索引起来，没改过的文件直接复用（不读标签）
        old = {t.path: t for t in self.tracks} if self.tracks else {}
        for root in self.roots:
            found.extend(self._collect(root, seen, old))
        self.tracks = found
        self.by_id = {t.tid: t for t in found}
        self.loaded_at = int(t0)
        log.info("音乐库索引完成：%d 首（复用 %d，新读 %d），用时 %.1fs",
                 len(found), self._reused, len(found) - self._reused,
                 time.time() - t0)
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

    def add_root(self, path, force=False):
        """新增音乐目录：先验证可见性，再写回 config.json

        force=True 只由控制台（在宿主机上）发起：容器是按曲库目录逐条挂载的，
        新目录在容器里暂时看不见属正常情况，先记进配置，重启应用后才会挂进来。
        """
        path = (path or "").strip().rstrip("/")
        if not path:
            return {"ok": False, "error": "路径为空"}
        if not os.path.isabs(path):
            return {"ok": False, "error": "请填绝对路径，例如 /vol1/1000/音乐"}
        if path in self.roots:
            return {"ok": False, "error": "这个路径已经加过了"}
        needs_restart = False
        if not os.path.isdir(path):
            if not force:
                return {"ok": False,
                        "error": "容器里看不到这个目录：要么路径写错了，"
                                 "要么是还没挂进容器的新目录（需重启应用）",
                        "need_mount": True}
            needs_restart = True
        elif not os.access(path, os.R_OK):
            return {"ok": False,
                    "error": "目录存在但没有读权限（NAS 上用 ACL 授权给运行用户）"}
        self.roots.append(path)
        self.cfg.set("music", "roots", value=list(self.roots))
        self.cfg.save()
        log.info("新增音乐目录 %s（需重启=%s）", path, needs_restart)
        return {"ok": True, "roots": self.roots, "needs_restart": needs_restart}

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
            self.tracks = [Track(d["path"], d["name"], d["artist"], d["album"],
                                 d.get("genre", ""), d.get("mtime", 0))
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
                                       "artist": t.artist, "album": t.album,
                                       "genre": t.genre,
                                       "mtime": round(t.mtime, 3)}
                                      for t in self.tracks]},
                          f, ensure_ascii=False)
        except Exception as e:
            log.warning("音乐索引落盘失败：%s", e)

    # ---------- 检索 ----------
    def genres(self):
        """各流派有多少首（空流派不计）"""
        from collections import Counter
        c = Counter(t.genre for t in self.tracks if t.genre)
        return c.most_common()

    def pick_genre(self, genre, mode="shuffle", limit=30):
        """按流派取歌"""
        g = (genre or "").strip().lower()
        tracks = [t for t in self.tracks if g and t.genre.lower() == g]
        if not tracks:
            tracks = [t for t in self.tracks if g and g in t.genre.lower()]
        if mode == "shuffle" and len(tracks) > 1:
            random.shuffle(tracks)
        if limit and limit > 0:
            tracks = tracks[:limit]
        return tracks

    def snapshot(self):
        """轻量指纹：音频文件数 + 最近一次修改时间

        用来判断「值不值得重扫」——曲库没变时重扫 1900 首要十几秒到两分钟，
        全白费。这里只遍历一遍文件系统，不读标签、不建索引。
        """
        n = 0
        latest = 0.0
        for root in self.roots:
            for dp, dns, fns in os.walk(root, onerror=lambda e: None):
                dns[:] = [d for d in dns if not d.startswith(("@", "."))]
                for fn in fns:
                    if not fn.lower().endswith(self.exts):
                        continue
                    n += 1
                    try:
                        m = os.path.getmtime(os.path.join(dp, fn))
                    except OSError:
                        continue
                    if m > latest:
                        latest = m
        return (n, round(latest, 3))

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
        # 只要有人命中「歌手或歌名」，就别把仅靠专辑名撞上的也放进来。
        # 专辑标签一旦有偏差，搜「伍佰」会带出一堆别人的歌。
        if scored and scored[0][0] > 0:
            scored = [x for x in scored if x[0] > 0]
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
