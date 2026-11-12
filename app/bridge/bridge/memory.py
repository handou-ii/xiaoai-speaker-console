"""播放历史与偏好记忆

三件事：
  1. play_event —— 放过什么、跳过什么，是偏好数据的来源
  2. turn       —— 最近几句指令，是「他 / 那首 / 再换一首」能被解析的前提
  3. context()  —— 压成一段参考资料，喂给直连模型

两条铁律：
  - 任何异常都吞掉，绝不因为记忆坏了放不出歌
  - 参考资料以「不是指令」的身份进入对话，不拼进 system（防注入：
    历史里存的是用户原话，塞进 system 就等于把用户的话变成规则）
"""
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("memory")

DAY = 86400.0
HALF_LIFE_DAYS = 30.0          # 30 天半衰：老偏好不能一直压着新口味

DEFAULTS = {
    "enabled": True,
    "path": "./conf/memory.db",
    "history_limit": 500,      # 事件最多留 500 条
    "history_days": 30,        # 超过 30 天的事件清掉
    "context_turns": 3,        # 带几句上文
    "recent_plays": 8,         # 带几首最近播放
    "context_chars": 400,      # 参考资料总长上限，避免拖慢首字
    "skip_min_sec": 40,        # 播不足这么多秒就切歌，算跳过
    "top_artists": 5,
    "top_skips": 3,
}

# 动作 -> 分数增量
SCORE = {"play": 1.0, "finish": 2.0, "skip": -2.0}


class Memory:
    def __init__(self, cfg):
        m = dict(DEFAULTS)
        try:
            m.update(cfg.raw.get("memory") or {})
        except Exception:
            pass
        self.enabled = bool(m.get("enabled", True))
        self.path = cfg.abspath(m.get("path") or "./conf/memory.db")
        self.limit = int(m.get("history_limit") or 500)
        self.days = float(m.get("history_days") or 30)
        self.turns = int(m.get("context_turns") or 3)
        self.recent = int(m.get("recent_plays") or 8)
        self.max_chars = int(m.get("context_chars") or 400)
        self.skip_min = float(m.get("skip_min_sec") or 40)
        self.top_artists = int(m.get("top_artists") or 5)
        self.top_skips = int(m.get("top_skips") or 3)
        self._lock = threading.Lock()
        self._con = None
        self._last = None          # 最近一次 play：{"tid","name","artist","ts"}
        if self.enabled:
            self._init()

    # ---------- 基础设施 ----------
    def _init(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            con = self._conn()
            con.executescript("""
            create table if not exists play_event(
                id integer primary key autoincrement,
                tid text, name text, artist text, genre text,
                action text, ts real
            );
            create index if not exists idx_event_ts on play_event(ts);
            create index if not exists idx_event_tid on play_event(tid);
            create table if not exists agg(
                kind text, key text, score real, hits integer, updated real,
                primary key(kind, key)
            );
            create table if not exists turn(
                id integer primary key autoincrement,
                query text, intent text, reply text, action text, ts real
            );
            create index if not exists idx_turn_ts on turn(ts);
            """)
            con.commit()
            log.info("记忆库就绪：%s", self.path)
        except Exception as e:
            log.warning("记忆库初始化失败（%s），本次运行不记历史", e)
            self.enabled = False

    def _conn(self):
        if self._con is None:
            self._con = sqlite3.connect(self.path, timeout=5,
                                        check_same_thread=False)
            self._con.execute("pragma journal_mode=wal")
            self._con.execute("pragma synchronous=normal")
        return self._con

    # ---------- 写入 ----------
    def record_play(self, track, dur=None):
        """开始播一首"""
        if not self.enabled or track is None:
            return
        tid = getattr(track, "tid", "") or ""
        name = getattr(track, "name", "") or ""
        artist = getattr(track, "artist", "") or ""
        genre = getattr(track, "genre", "") or ""
        self._last = {"tid": tid, "name": name, "artist": artist,
                      "ts": time.time(), "dur": float(dur or 0)}
        self._write_event(track, "play")
        self._bump("artist", artist, SCORE["play"])

    def record_finish(self, track=None):
        """完整播完"""
        if not self.enabled:
            return
        t = track or (self._last if self._last else None)
        if not t:
            return
        if track is None:
            self._write_flat(t, "finish")
        else:
            self._write_event(track, "finish")
        self._bump("artist", t.get("artist") if isinstance(t, dict)
                   else getattr(t, "artist", ""), SCORE["finish"])

    def record_skip(self, track=None):
        """切歌了。播得够久算自然听完，够短才算不喜欢"""
        if not self.enabled:
            return
        last = self._last
        if track is not None and not isinstance(track, dict):
            cur = {"tid": getattr(track, "tid", ""),
                   "name": getattr(track, "name", ""),
                   "artist": getattr(track, "artist", ""),
                   "genre": getattr(track, "genre", "")}
        else:
            cur = track or last
        if not cur:
            return
        played = (time.time() - last["ts"]) if last else 0.0
        # 门槛取「40 秒」和「本曲 30%」里更小的那个：30 秒的短歌自然播完
        # 不该被判成讨厌
        limit = self.skip_min
        if last and last.get("dur"):
            limit = min(limit, last["dur"] * 0.3)
        if last and played >= limit:
            # 听了一阵才切，说明不是讨厌，按「听过」记正分
            self._write_flat(last, "finish")
            self._bump("artist", last.get("artist", ""), SCORE["finish"])
        else:
            self._write_flat(cur, "skip")
            self._bump("artist", cur.get("artist", ""), SCORE["skip"])
            log.info("记一次跳过：%s（播了 %.0fs）", cur.get("name", ""), played)
        self._last = None

    def record_turn(self, query, intent="", reply="", action=""):
        """记一句指令，供下一句解析指代"""
        if not self.enabled:
            return
        try:
            with self._lock:
                con = self._conn()
                con.execute(
                    "insert into turn(query,intent,reply,action,ts) values(?,?,?,?,?)",
                    ((query or "")[:300], (intent or "")[:200],
                     (reply or "")[:200], action or "", time.time()))
                con.commit()
        except Exception as e:
            log.debug("记指令失败：%s", e)

    def _write_event(self, track, action):
        self._write_flat({"tid": getattr(track, "tid", ""),
                          "name": getattr(track, "name", ""),
                          "artist": getattr(track, "artist", ""),
                          "genre": getattr(track, "genre", "")}, action)

    def _write_flat(self, d, action):
        try:
            with self._lock:
                con = self._conn()
                con.execute(
                    "insert into play_event(tid,name,artist,genre,action,ts)"
                    " values(?,?,?,?,?,?)",
                    (d.get("tid", ""), d.get("name", ""), d.get("artist", ""),
                     d.get("genre", ""), action, time.time()))
                con.commit()
                self._prune(con)
        except Exception as e:
            log.debug("写播放事件失败：%s", e)

    def _bump(self, kind, key, delta):
        key = (key or "").strip()
        if not key:
            return
        try:
            with self._lock:
                con = self._conn()
                now = time.time()
                row = con.execute(
                    "select score, updated from agg where kind=? and key=?",
                    (kind, key)).fetchone()
                if row:
                    # 先把旧分按半衰折到当下，再叠加
                    old = row[0] * (0.5 ** ((now - row[1]) / (HALF_LIFE_DAYS * DAY)))
                    con.execute(
                        "update agg set score=?, hits=hits+1, updated=?"
                        " where kind=? and key=?",
                        (old + delta, now, kind, key))
                else:
                    con.execute(
                        "insert into agg(kind,key,score,hits,updated) values(?,?,?,1,?)",
                        (kind, key, delta, now))
                con.commit()
        except Exception as e:
            log.debug("更新偏好失败：%s", e)

    def _prune(self, con):
        """先按天删，再按条数截 —— 两条都要，缺一条都不够"""
        try:
            now = time.time()
            cutoff = now - self.days * DAY
            con.execute("delete from play_event where ts < ?", (cutoff,))
            con.execute("delete from turn where ts < ?", (cutoff,))
            n = con.execute("select count(*) from play_event").fetchone()[0]
            if n > self.limit:
                con.execute(
                    "delete from play_event where id in ("
                    " select id from play_event order by id asc limit ?)",
                    (n - self.limit,))
        except Exception as e:
            log.debug("清理历史失败：%s", e)

    # ---------- 读取 ----------
    def _decayed(self, kind, sign, limit):
        """按当下衰减后的分数取榜单。sign>0 取喜欢的，<0 取被跳过的"""
        try:
            with self._lock:
                con = self._conn()
                rows = con.execute(
                    "select key, score, hits, updated from agg where kind=?",
                    (kind,)).fetchall()
        except Exception:
            return []
        now = time.time()
        out = []
        for key, score, hits, upd in rows:
            cur = score * (0.5 ** ((now - upd) / (HALF_LIFE_DAYS * DAY)))
            if sign > 0 and cur > 0:
                out.append((key, cur, hits))
            elif sign < 0 and cur < 0:
                out.append((key, -cur, hits))
        out.sort(key=lambda x: -x[1])
        return out[:limit]

    def recent_plays(self, limit=None):
        # 同一首会被记多次（play + finish/skip），只留最后一次的状态，
        # 否则列表里每首歌都出现两遍
        try:
            with self._lock:
                con = self._conn()
                rows = con.execute(
                    "select name, artist, action from play_event"
                    " where id in (select max(id) from play_event group by tid)"
                    " order by id desc limit ?",
                    (limit or self.recent,)).fetchall()
        except Exception:
            return []
        return rows

    def recent_turns(self, limit=None):
        try:
            with self._lock:
                con = self._conn()
                rows = con.execute(
                    "select query, reply from turn order by id desc limit ?",
                    (limit or self.turns,)).fetchall()
        except Exception:
            return []
        return list(reversed(rows))

    def context(self):
        """压成一段参考资料。没有数据就返回空串，调用方按原样走"""
        if not self.enabled:
            return ""
        try:
            parts = []

            turns = self.recent_turns()
            if turns:
                lines = []
                for q, r in turns:
                    q = (q or "").strip()
                    r = (r or "").strip()
                    if not q:
                        continue
                    lines.append("「%s」→ %s" % (q[:40], (r[:40] or "已处理")))
                if lines:
                    parts.append("【刚才】" + " / ".join(lines))

            plays = self.recent_plays()
            if plays:
                names = []
                for name, artist, action in plays:
                    tag = "（跳过）" if action == "skip" else ""
                    names.append("%s-%s%s" % (artist or "?", name or "?", tag))
                parts.append("【最近播放】" + "、".join(names))

            liked = self._decayed("artist", 1, self.top_artists)
            if liked:
                parts.append("【常听】" + "、".join(
                    "%s(%d)" % (k, int(v)) for k, v, _ in liked))

            disliked = self._decayed("artist", -1, self.top_skips)
            if disliked:
                parts.append("【常被跳过】" + "、".join(k for k, _, _ in disliked))

            text = "\n".join(parts)
            if len(text) > self.max_chars:
                text = text[:self.max_chars]
            return text
        except Exception as e:
            log.debug("构造参考资料失败：%s", e)
            return ""

    def stats(self):
        """给 WebUI 看的一眼数据"""
        out = {"enabled": self.enabled, "path": self.path,
               "events": 0, "turns": 0, "artists": 0}
        if not self.enabled:
            return out
        try:
            with self._lock:
                con = self._conn()
                out["events"] = con.execute(
                    "select count(*) from play_event").fetchone()[0]
                out["turns"] = con.execute(
                    "select count(*) from turn").fetchone()[0]
                out["artists"] = con.execute(
                    "select count(*) from agg where kind='artist'").fetchone()[0]
        except Exception:
            pass
        return out
