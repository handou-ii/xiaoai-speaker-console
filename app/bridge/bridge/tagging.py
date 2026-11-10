"""按曲打标签（画像）：把「这首歌是什么感觉」变成可检索的数据

为什么不词表、不靠音频特征：
  * 意图词表是开放集合，加不完（安静、city pop、一听就想睡…）；
  * 响度只能测「母带多响」，测不出节奏与情绪，而且镜像要涨几百兆。
这里改成给每首歌生成一份受控枚举的画像，存成本地 JSON，
检索时按字段过滤——毫秒级，且每条判断都能解释、能手改。

画像字段（取值固定，不让模型自由发挥，否则几千首会出现几百种写法）：
  mood   情绪：平静/温柔/伤感/治愈/热血/欢快/忧郁/孤独/浪漫/神秘
  tempo  节奏：慢/中/快
  vocal  人声：纯器乐/有歌词
  scene  场景：睡前/工作/通勤/运动/雨天/独处/聚会
  energy 能量：低/中/高
"""

import asyncio
import json
import logging
import os
import random
import time

log = logging.getLogger("tagging")

MOODS = ("平静", "温柔", "伤感", "治愈", "热血", "欢快", "忧郁", "孤独", "浪漫", "神秘")
TEMPOS = ("慢", "中", "快")
VOCALS = ("纯器乐", "有歌词")
SCENES = ("睡前", "工作", "通勤", "运动", "雨天", "独处", "聚会")
ENERGIES = ("低", "中", "高")

BATCH = 40            # 一次请求标注多少首（实测 40 首一轮 6–8 秒，再大容易漏行）
CONCURRENCY = 2       # 同时几轮，别把端点打满
SAVE_EVERY = 5        # 每几批落一次盘，中断也不丢太多

SYSTEM = (
    "你是音乐资料整理员。根据曲名、歌手、流派推断每首歌的画像。"
    "取值只能从下面选（情绪与场景可多选，最多 3 个）：\n"
    "情绪: %s\n节奏: %s\n人声: %s\n场景: %s\n能量: %s\n"
    "推断不出来就把对应字段填 \"未知\"，不要编造。"
    "只输出 JSON 数组，不要任何解释。"
    % ("/".join(MOODS), "/".join(TEMPOS), "/".join(VOCALS),
       "/".join(SCENES), "/".join(ENERGIES))
)


def _pick_list(raw, allowed, limit=3):
    if not isinstance(raw, (list, tuple)):
        raw = [raw] if raw else []
    out = []
    for v in raw:
        v = str(v).strip()
        if v in allowed and v not in out:
            out.append(v)
    return out[:limit]


def _pick_one(raw, allowed):
    v = str(raw or "").strip()
    return v if v in allowed else "未知"


class Tagger:
    def __init__(self, cfg, library, llm):
        self.cfg = cfg
        self.library = library
        self.llm = llm
        self.path = cfg.abspath("./conf/track_tags.json")
        self.data = {}
        self.running = False
        self.done = 0
        self.total = 0
        self.error = ""
        self.started_at = 0.0
        self._task = None
        self.batch_size = self._int_cfg("tagging.batch", BATCH, 5, 60)
        self.concurrency = self._int_cfg("tagging.concurrency", CONCURRENCY, 1, 6)
        self.load()

    def _int_cfg(self, key, default, low, high):
        try:
            v = int((self.cfg.get(key) or default))
        except (TypeError, ValueError):
            v = default
        return max(low, min(high, v))

    # ---------- 缓存 ----------
    def load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = d
                log.info("画像缓存加载：%d 首", len(self.data))
        except Exception:
            log.exception("画像缓存读取失败，从空的开始")
            self.data = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            log.exception("画像缓存写入失败")

    # ---------- 状态 ----------
    def status(self):
        total = len(self.library.tracks)
        covered = sum(1 for t in self.library.tracks if t.tid in self.data)
        return {
            "ok": True,
            "running": self.running,
            "total": total,
            "covered": covered,
            "done": self.done,
            "queued": self.total,
            "error": self.error,
            "llm": bool(self.llm and getattr(self.llm, "configured", False)),
        }

    def tags_of(self, track):
        return self.data.get(getattr(track, "tid", "")) if track else None

    def start(self, force=False):
        if self.running:
            return {"ok": True, "started": False, "message": "画像生成已在运行"}
        if not (self.llm and getattr(self.llm, "configured", False)):
            return {"ok": False, "error": "模型还没配置，先到「AI 模型」页填好"}
        self.running = True
        self.done = 0
        self.total = 0
        self.error = ""
        self.started_at = time.time()
        self._task = asyncio.get_event_loop().create_task(self._run(force))
        return {"ok": True, "started": True}

    # ---------- 打标 ----------
    async def _run(self, force=False):
        try:
            self.library.scan()
            todo = [t for t in self.library.tracks
                    if force or t.tid not in self.data]
            self.total = len(todo)
            log.info("画像生成开始：%d 首待标，批次 %d，并发 %d",
                     self.total, self.batch_size, self.concurrency)
            if not todo:
                return
            sem = asyncio.Semaphore(self.concurrency)
            lock = asyncio.Lock()
            batches = [todo[i:i + self.batch_size]
                       for i in range(0, len(todo), self.batch_size)]
            counter = {"n": 0}

            async def one(rows):
                async with sem:
                    try:
                        out = await self._tag_batch(rows)
                    except Exception as e:
                        out = {}
                        self.error = str(e)[:140]
                        log.warning("画像批次失败：%s", self.error)
                    async with lock:
                        self.data.update(out)
                        self.done += len(rows)
                        counter["n"] += 1
                        if counter["n"] % SAVE_EVERY == 0:
                            self.save()
                            log.info("画像进度 %d/%d", self.done, self.total)

            await asyncio.gather(*(one(b) for b in batches))
            self.save()
            log.info("画像生成结束：%d/%d 已标", self.done, self.total)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)[:140]
            log.exception("画像生成失败")
        finally:
            self.running = False

    async def _tag_batch(self, rows):
        lines = ["%d | %s | %s | %s" % (i, t.artist or "未知", t.name, t.genre or "无")
                 for i, t in enumerate(rows, 1)]
        prompt = ("下面 %d 首歌，逐条给画像：\n%s\n\n"
                  "只输出数组，格式：\n"
                  '[{"i":1,"mood":["平静"],"tempo":"慢","vocal":"纯器乐",'
                  '"scene":["睡前"],"energy":"低"}]'
                  % (len(rows), "\n".join(lines)))
        r = await self.llm.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}], timeout=90)
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or "模型无返回")
        txt = r.get("reply") or ""
        start, end = txt.find("["), txt.rfind("]")
        if start < 0 or end <= start:
            raise RuntimeError("模型没给出 JSON 数组")
        arr = json.loads(txt[start:end + 1])
        out = {}
        now = int(time.time())
        for x in arr:
            if not isinstance(x, dict):
                continue
            try:
                i = int(x.get("i")) - 1
            except (TypeError, ValueError):
                continue
            if not (0 <= i < len(rows)):
                continue
            t = rows[i]
            out[t.tid] = {
                "mood": _pick_list(x.get("mood"), MOODS) or ["未知"],
                "tempo": _pick_one(x.get("tempo"), TEMPOS),
                "vocal": _pick_one(x.get("vocal"), VOCALS),
                "scene": _pick_list(x.get("scene"), SCENES) or ["未知"],
                "energy": _pick_one(x.get("energy"), ENERGIES),
                "at": now,
            }
        if len(out) < len(rows):
            log.info("这一批 %d 首只返回 %d 条，剩下的下次补标", len(rows), len(out))
        return out

    # ---------- 检索 ----------
    def _hits(self, want):
        moods = set(want.get("mood") or [])
        scenes = set(want.get("scene") or [])
        tempo = want.get("tempo") or ""
        vocal = want.get("vocal") or ""
        energy = want.get("energy") or ""
        out = []
        for t in self.library.tracks:
            tg = self.data.get(t.tid)
            if not tg:
                continue
            if moods and not (set(tg.get("mood") or []) & moods):
                continue
            if scenes and not (set(tg.get("scene") or []) & scenes):
                continue
            if tempo and tg.get("tempo") != tempo:
                continue
            if vocal and tg.get("vocal") != vocal:
                continue
            if energy and tg.get("energy") != energy:
                continue
            out.append(t)
        return out

    def search(self, want, limit=20):
        """按画像检索。命中太少就逐级放宽，并返回放宽了哪些条件

        放宽顺序是有意的：先松场景（最主观），再松节奏、人声，
        最后才松情绪——情绪是用户话里最核心的那部分。
        """
        cur = {k: v for k, v in (want or {}).items() if v}
        if not cur:
            return [], []
        pool = self._hits(cur)
        dropped = []
        for k in ("scene", "tempo", "vocal"):
            if len(pool) >= max(5, min(limit, 10)):
                break
            if k in cur:
                cur.pop(k)
                dropped.append(k)
                pool = self._hits(cur)
        if len(pool) < 5 and "mood" in cur:
            cur.pop("mood")
            dropped.append("mood")
            pool = self._hits(cur)
        random.shuffle(pool)
        return pool[:limit], dropped

    def similar(self, track, limit=20):
        """找「语境相近」的歌：画像重叠度打分，同流派再加一点"""
        base = self.data.get(getattr(track, "tid", ""))
        if not base:
            return []
        b_mood = set(base.get("mood") or [])
        b_scene = set(base.get("scene") or [])
        scored = []
        for t in self.library.tracks:
            if t.tid == getattr(track, "tid", ""):
                continue
            tg = self.data.get(t.tid)
            if not tg:
                continue
            s = 3 * len(b_mood & set(tg.get("mood") or []))
            s += 2 * (tg.get("tempo") == base.get("tempo"))
            s += 2 * (tg.get("energy") == base.get("energy"))
            s += 2 * len(b_scene & set(tg.get("scene") or []))
            s += 1 * (tg.get("vocal") == base.get("vocal"))
            if t.genre and t.genre == getattr(track, "genre", ""):
                s += 1
            if s > 0:
                scored.append((s, t))
        if not scored:
            return []
        scored.sort(key=lambda x: -x[0])
        top = [t for _, t in scored[:max(limit * 3, 60)]]
        random.shuffle(top)
        return top[:limit]
