# -*- coding: utf-8 -*-
"""
PRD 可交互原型 · 数据服务（自包含版）
读 data/articles.json（采集数据快照），无外部数据库依赖，仅用 Python 标准库。

启动: py server.py [端口]   （默认 8091）
访问: http://127.0.0.1:8091/
API:
  /api/overview              工作台 KPI
  /api/topics                监测主题列表
  /api/articles?topic&media&sentiment&q&limit   文章列表
  /api/hot                   热词榜
  /api/assistant?q           智能助手（规则分析数据）
  /api/themes POST           保存或更新监测主题
  /api/plan-route POST       应对方案模板规则推荐（需六维）
  /api/plan POST             按已确认模板生成应对方案/分型报告
"""
import os
import sys
import json
import re
import copy
import datetime as dt
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))

# 情感分析引擎（可插拔：rule/aliyun/xfyun/selfhosted）与 LLM 归纳（态度/立场/情感迁移/理由）
try:
    import sentiment as _sentiment
except Exception:
    _sentiment = None
try:
    import insight_llm as _insight
except Exception:
    _insight = None
try:
    import collect as _collect
except Exception:
    _collect = None
try:
    import clean as _clean
except Exception:
    _clean = None
DATA_FILE = os.path.join(_HERE, "data", "articles.json")
THEMES_FILE = os.path.join(_HERE, "data", "themes.json")
CONFIG_FILE = os.path.join(_HERE, "data", "config.json")

# config.json 字段 → 环境变量映射（设置页保存后即时生效，供 sentiment.py / insight_llm.py 运行时读取）
CONFIG_KEYS = [
    ("sentiment_provider", "SENTIMENT_PROVIDER"),
    ("aliyun_ak_id", "ALIYUN_AK_ID"),
    ("aliyun_ak_secret", "ALIYUN_AK_SECRET"),
    ("xfyun_appid", "XFYUN_APPID"),
    ("xfyun_apikey", "XFYUN_APIKEY"),
    ("selfhosted_url", "SENTIMENT_SELFHOSTED_URL"),
    ("llm_base_url", "LLM_BASE_URL"),
    ("llm_api_key", "LLM_API_KEY"),
    ("llm_model", "LLM_MODEL"),
]
_SENSITIVE = {"aliyun_ak_secret", "xfyun_apikey", "llm_api_key"}


def _apply_config(cfg):
    """把 config 注入环境变量，非空才写（空不覆盖已有值）。"""
    for ckey, envkey in CONFIG_KEYS:
        val = cfg.get(ckey)
        if val:
            os.environ[envkey] = str(val).strip()


def _public_config(cfg):
    """返回脱敏后的配置（密钥只回传是否已配置 + 末四位提示）。"""
    out = {}
    for ckey, _env in CONFIG_KEYS:
        v = (cfg.get(ckey) or "").strip()
        if ckey in _SENSITIVE:
            out[ckey] = {"configured": bool(v), "hint": (v[-4:] if len(v) >= 4 else "")}
        else:
            out[ckey] = v
    return out


def _load_config_file():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    _apply_config(cfg)
    return cfg


def _save_config(data):
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    for ckey, _env in CONFIG_KEYS:
        if ckey in data and ckey not in _SENSITIVE:
            cfg[ckey] = (data.get(ckey) or "").strip()
        elif ckey in data and ckey in _SENSITIVE:
            val = (data.get(ckey) or "").strip()
            if val:  # 密钥留空 = 保留原值
                cfg[ckey] = val
    with open(CONFIG_FILE, "w", encoding="utf-8-sig") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    _apply_config(cfg)
    return cfg

# 监测主题：关键词过滤数据
THEMES = [
    {"id": "t_ai", "name": "AI 大模型", "keywords": ["AI", "大模型", "DeepSeek", "Claude", "GPT", "人工智能"],
     "meta": "科技数码 · 运行中", "status": "运行中"},
    {"id": "t_apple", "name": "苹果生态", "keywords": ["苹果", "iPhone", "iOS", "watchOS", "Apple"],
     "meta": "科技数码 · 运行中", "status": "运行中"},
    {"id": "t_mi", "name": "小米数码", "keywords": ["小米", "REDMI"],
     "meta": "科技数码 · 运行中", "status": "运行中"},
    {"id": "t_hw", "name": "华为终端", "keywords": ["华为", "鸿蒙"],
     "meta": "科技数码 · 运行中", "status": "运行中"},
    {"id": "t_car", "name": "新能源汽车", "keywords": ["汽车", "新能源", "特斯拉", "比亚迪"],
     "meta": "产业 · 运行中", "status": "运行中"},
    {"id": "t_hot", "name": "全网热搜", "keywords": [], "source_only": "baidu_hot",
     "meta": "百度热搜 · 运行中", "status": "运行中"},
]
THEMES_BUILTIN = copy.deepcopy(THEMES)

SRC_NAME = {"it_home": "IT之家", "sspai": "少数派", "baidu_hot": "百度热搜",
             "ifanr": "爱范儿", "geekpark": "极客公园", "leiphone": "雷锋网",
             "weibo_hot": "微博热搜", "zhihu_hot": "知乎热榜"}
SRC_MEDIA = {"it_home": "news", "sspai": "news", "baidu_hot": "hot",
             "ifanr": "news", "geekpark": "news", "leiphone": "news",
             "weibo_hot": "hot", "zhihu_hot": "hot"}
SENT_MAP = {"正面": "pos", "中性": "neu", "负面": "neg"}

# 数据源别名：主题名/关键词命中这些别名时，按数据源（而非关键词）监测
SRC_ALIAS = {
    "微博热搜": "weibo_hot", "微博": "weibo_hot",
    "知乎热榜": "zhihu_hot", "知乎": "zhihu_hot",
    "百度热搜": "baidu_hot", "百度": "baidu_hot",
    "IT之家": "it_home", "ithome": "it_home",
    "少数派": "sspai",
    "爱范儿": "ifanr", "极客公园": "geekpark", "雷锋网": "leiphone",
}

# 数据装载
ARTICLES = []


def _score_one(r):
    """用情感引擎对单条文章重新打标（正/中/负 + score + confidence）。"""
    text = (r.get("title") or "") + "。" + (r.get("content") or "")
    senti = _sentiment.analyze(text)["sentiment"]
    r["sentiment_label"] = senti["label"]
    r["sentiment_score"] = senti["score"]
    r["sentiment_confidence"] = senti["confidence"]
    return r


def _sentiment_snapshot():
    """返回当前文章的情感分布 + 每篇详情（纯读取，不重算）。"""
    e = {"正面": 0, "中性": 0, "负面": 0}
    for r in ARTICLES:
        e[r.get("sentiment_label", "中性")] = e.get(r.get("sentiment_label", "中性"), 0) + 1
    total = max(1, sum(e.values()))
    order = {"负面": 0, "中性": 1, "正面": 2}
    items = [{
        "id": r.get("id"),
        "title": r.get("title"),
        "source": SRC_NAME.get(r.get("source"), r.get("source")),
        "sentiment_label": r.get("sentiment_label", "中性"),
        "score": r.get("sentiment_score"),
        "confidence": r.get("sentiment_confidence"),
    } for r in ARTICLES]
    items.sort(key=lambda x: (order.get(x["sentiment_label"], 9), -(x["score"] or 0)))
    return {
        "provider": os.environ.get("SENTIMENT_PROVIDER") or "rule",
        "total": len(ARTICLES),
        "emotion": e,
        "ratio": {k: round(v / total * 100, 1) for k, v in e.items()},
        "items": items,
    }


def _recompute():
    """全量重算情感后返回快照。无引擎时返回 None。"""
    global ARTICLES
    if not _sentiment:
        return None
    for r in ARTICLES:
        _score_one(r)
    return _sentiment_snapshot()


def _split_kws(val):
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = (val or "").strip()
    if not s:
        return []
    return [x for x in re.split(r"[\s+|/]+", s) if x]


def _apply_theme_record(theme, rec):
    """用 themes.json 一条记录覆盖主题字段。"""
    if rec.get("name"):
        theme["name"] = rec["name"].strip()
    if "keywords" in rec:
        theme["keywords"] = _split_kws(rec.get("keywords"))
        blob = (theme.get("name") or "") + " " + " ".join(theme["keywords"])
        matched_src = None
        for alias, src in SRC_ALIAS.items():
            if alias.lower() in blob.lower():
                matched_src = src
                break
        if matched_src:
            theme["source_only"] = matched_src
            theme["meta"] = f"{SRC_NAME.get(matched_src, matched_src)} · 运行中"
        else:
            builtin = next((t for t in THEMES_BUILTIN if t["id"] == theme.get("id")), None)
            if builtin and builtin.get("source_only"):
                theme["source_only"] = builtin["source_only"]
            else:
                theme.pop("source_only", None)
    if "exclude" in rec:
        theme["exclude"] = _split_kws(rec.get("exclude"))
    group = (rec.get("group") or "").strip()
    if group:
        theme["group"] = group
        theme["meta"] = f"{group} · {theme.get('status') or '运行中'}"
    if "alert" in rec:
        theme["alert"] = bool(rec.get("alert"))
    return theme


def _new_theme_from_record(rec, tid):
    theme = {
        "id": tid,
        "name": (rec.get("name") or "").strip() or "未命名",
        "keywords": [],
        "exclude": [],
        "meta": f"{rec.get('group') or '自定义'} · 运行中",
        "status": "运行中",
        "group": rec.get("group") or "自定义",
    }
    return _apply_theme_record(theme, rec)


def _load_custom_themes():
    """从 themes.json 覆盖内置主题、合并自定义主题。"""
    global THEMES
    base = copy.deepcopy(THEMES_BUILTIN)
    by_id = {t["id"]: t for t in base}
    if not os.path.exists(THEMES_FILE):
        THEMES = base
        return
    try:
        with open(THEMES_FILE, encoding="utf-8-sig") as f:
            customs = json.load(f)
    except Exception:
        THEMES = base
        return
    if not isinstance(customs, list):
        THEMES = base
        return
    extra = []
    for c in customs:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        name = (c.get("name") or "").strip()
        if cid and cid in by_id:
            _apply_theme_record(by_id[cid], c)
            continue
        if not name:
            continue
        if any(t["name"] == name for t in base) and not cid:
            continue
        tid = cid if cid.startswith("c_") else f"c_{len(extra) + 1}"
        extra.append(_new_theme_from_record(c, tid))
    THEMES = list(by_id.values()) + extra


def _read_themes_file():
    if not os.path.exists(THEMES_FILE):
        return []
    try:
        with open(THEMES_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_themes_file(rows):
    folder = os.path.dirname(THEMES_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(THEMES_FILE, "w", encoding="utf-8-sig") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _save_theme(body):
    """新建或更新监测主题，写入 themes.json 后重新装载。"""
    name = (body.get("name") or "").strip()
    keywords = body.get("keywords") if body.get("keywords") is not None else ""
    if isinstance(keywords, list):
        keywords = "|".join(str(x) for x in keywords if str(x).strip())
    keywords = str(keywords).strip()
    if not name:
        return {"ok": False, "error": "请填写方案名称"}
    tid = str(body.get("id") or "").strip()
    rec = {
        "name": name,
        "group": (body.get("group") or "").strip() or "自定义",
        "keywords": keywords,
        "exclude": (body.get("exclude") or "").strip(),
        "alert": bool(body.get("alert")),
        "update_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    rows = _read_themes_file()
    if tid:
        rec["id"] = tid
        found = False
        for i, r in enumerate(rows):
            if str(r.get("id") or "") == tid:
                rec["create_time"] = r.get("create_time") or rec["update_time"]
                rows[i] = rec
                found = True
                break
        if not found:
            rec["create_time"] = rec["update_time"]
            rows.append(rec)
    else:
        rec["id"] = "c_" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
        rec["create_time"] = rec["update_time"]
        rows.append(rec)
        tid = rec["id"]
    _write_themes_file(rows)
    _load_custom_themes()
    return {"ok": True, "id": tid}


def _load():
    global ARTICLES
    with open(DATA_FILE, encoding="utf-8-sig") as f:
        rows = json.load(f)
    for r in rows:
        if isinstance(r.get("publish_time"), str):
            try:
                r["publish_time"] = dt.datetime.strptime(r["publish_time"][:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                r["publish_time"] = dt.datetime.now()
        r.setdefault("content", "")
        r.setdefault("author", "")
        r.setdefault("url", "")
        r.setdefault("sentiment_label", "中性")
        # 传播数据预留字段（当前采集源不含互动量，后续接入平台 API/第三方数据后填充）
        r.setdefault("read_count", 0)
        r.setdefault("comment_count", 0)
        r.setdefault("forward_count", 0)
    ARTICLES = rows
    # 可选：启动时用情感引擎重算（SENTIMENT_RECOMPUTE=1）；默认沿用 articles.json 预置标签
    recomputed = False
    if os.environ.get("SENTIMENT_RECOMPUTE") == "1" and _sentiment:
        _recompute()
        recomputed = True
    print(f"已装载 {len(ARTICLES)} 条文章" + ("（已用情感引擎重算）" if recomputed else "（沿用预置标签）"))
    _load_custom_themes()
    print(f"已装载 {len(THEMES)} 个监测主题")


def _rel_time(t):
    if not t:
        return "未知"
    if isinstance(t, str):
        try:
            t = dt.datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知"
    now = dt.datetime.now()
    delta = now - t
    if delta.total_seconds() < 0:
        return "刚刚"
    if delta.days == 0:
        if delta.seconds < 3600:
            return f"{max(1, delta.seconds // 60)} 分钟前"
        return f"{delta.seconds // 3600} 小时前"
    if delta.days == 1:
        return "昨天"
    return f"{delta.days} 天前"


def _match_theme(theme, row):
    text = (row.get("title") or "") + " " + (row.get("content") or "")[:200]
    for ex in theme.get("exclude") or []:
        if ex and ex.lower() in text.lower():
            return False
    if theme.get("source_only"):
        return row["source"] == theme["source_only"]
    return any(kw.lower() in text.lower() for kw in theme["keywords"])


def _assign_topic(row):
    for t in THEMES:
        if _match_theme(t, row):
            return t["id"]
    return "t_hot" if row["source"] == "baidu_hot" else None


def _shape(row):
    return {
        "id": row["id"],
        "topic": _assign_topic(row),
        "title": row["title"],
        "summary": (row.get("content") or "")[:90] + ("…" if len(row.get("content") or "") > 90 else ""),
        "source": SRC_NAME.get(row["source"], row["source"]),
        "media": SRC_MEDIA.get(row["source"], "news"),
        "time": _rel_time(row["publish_time"]),
        "sentiment": SENT_MAP.get(row["sentiment_label"], "neu"),
        "url": row.get("url"),
    }


def _articles_rows(media=None, sentiment=None, q=None):
    rows = ARTICLES
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r.get("title") or "").lower() or ql in (r.get("content") or "").lower()]
    if sentiment:
        label = {"pos": "正面", "neu": "中性", "neg": "负面"}[sentiment]
        rows = [r for r in rows if r["sentiment_label"] == label]
    if media:
        sources = [k for k, v in SRC_MEDIA.items() if v == media]
        rows = [r for r in rows if r["source"] in sources]
    return rows


def _overview():
    rows = ARTICLES
    n = len(rows)
    today = dt.date.today()
    today_n = sum(1 for r in rows if r.get("publish_time") and r["publish_time"].date() == today)
    e = {"正面": 0, "中性": 0, "负面": 0}
    for r in rows:
        e[r.get("sentiment_label", "中性")] = e.get(r.get("sentiment_label", "中性"), 0) + 1
    pos, neu, neg = e["正面"], e["中性"], e["负面"]
    total = max(1, pos + neu + neg)
    since = dt.date.today() - dt.timedelta(days=6)
    trend_map = {}
    for r in rows:
        t = r.get("publish_time")
        if t and t.date() >= since:
            trend_map[str(t.date())] = trend_map.get(str(t.date()), 0) + 1
    trend_days = []
    for i in range(6, -1, -1):
        trend_days.append(trend_map.get(str(since + dt.timedelta(days=i)), 0))
    attn = sorted(
        [r for r in rows if r.get("sentiment_label") == "负面"],
        key=lambda r: r.get("publish_time") or dt.datetime.min, reverse=True)[:5]
    return {
        "total": n,
        "today_add": today_n,
        "alert_count": neg,
        "positive_ratio": round(pos / total * 100, 1),
        "emotion": {"pos": pos, "neu": neu, "neg": neg},
        "trend_7d": trend_days,
        "attention": [{
            "id": a["id"], "title": a["title"],
            "meta": f"{SRC_NAME.get(a['source'], a['source'])} · {_rel_time(a['publish_time'])} · 负面",
            "topic": _assign_topic(a),
        } for a in attn],
    }


# 事件分析关键词（用于按事件过滤文章；后续可改为事件表查询）
EVENT_KEYWORDS = {
    "e1": ["关税", "美国", "加征", "出口"],
    "e2": ["召回", "缺陷", "投诉", "质量"],
}


def _scope_rows(scope):
    """按主题/事件范围过滤文章。scope：topic:<id> 或 event:<id>；空则返回全部。"""
    if not scope:
        return ARTICLES
    if scope.startswith("topic:"):
        tid = scope[6:]
        return [r for r in ARTICLES if _assign_topic(r) == tid]
    if scope.startswith("event:"):
        eid = scope[6:]
        kws = EVENT_KEYWORDS.get(eid, [])
        if not kws:
            return ARTICLES
        return [r for r in ARTICLES if any(
            kw.lower() in ((r.get("title") or "") + " " + (r.get("content") or "")).lower()
            for kw in kws)]
    return ARTICLES


def _metrics(scope=None):
    """六维报告用指标：声量趋势（近7天按天）+ 情感分布 + 来源构成。全部基于真实数据统计。"""
    rows = _scope_rows(scope)
    n = len(rows)
    e = {"正面": 0, "中性": 0, "负面": 0}
    for r in rows:
        e[r.get("sentiment_label", "中性")] = e.get(r.get("sentiment_label", "中性"), 0) + 1
    total = max(1, sum(e.values()))
    # 近7天按天声量
    since = dt.date.today() - dt.timedelta(days=6)
    trend_map = {}
    for r in rows:
        t = r.get("publish_time")
        if t and t.date() >= since:
            trend_map[str(t.date())] = trend_map.get(str(t.date()), 0) + 1
    trend = []
    for i in range(7):
        d = since + dt.timedelta(days=i)
        trend.append({"date": d.strftime("%m-%d"), "count": trend_map.get(str(d), 0)})
    # 来源构成（传播结构降级：基于来源统计，非完整传播链）
    src_map = {}
    for r in rows:
        s = r.get("source") or "未知"
        src_map[s] = src_map.get(s, 0) + 1
    source = [{"source": SRC_NAME.get(k, k), "count": v}
              for k, v in sorted(src_map.items(), key=lambda x: -x[1])]
    return {
        "total": n,
        "trend": trend,
        "emotion": {
            "pos": e["正面"], "neu": e["中性"], "neg": e["负面"], "total": total,
            "pos_ratio": round(e["正面"] / total * 100, 1),
            "neu_ratio": round(e["中性"] / total * 100, 1),
            "neg_ratio": round(e["负面"] / total * 100, 1),
        },
        "source": source,
    }


def _topics():
    out = []
    for t in THEMES:
        matched = [r for r in ARTICLES if _assign_topic(r) == t["id"]]
        out.append({**t, "count": len(matched)})
    return out


def _articles(theme_id, media, sentiment, q, limit):
    rows = _articles_rows(media=media, sentiment=sentiment, q=q)
    rows = sorted(rows, key=lambda r: r.get("publish_time") or dt.datetime.min, reverse=True)
    shaped = [_shape(r) for r in rows]
    if theme_id:
        shaped = [a for a in shaped if a["topic"] == theme_id]
    return shaped[:limit]


def _hot(limit=10):
    c = {}
    for r in ARTICLES:
        for w in re.findall(r"[一-龥]{2,6}", r.get("title") or ""):
            if w in {"什么", "如何", "为什么", "一个", "我们", "已经", "进行", "发布", "正式", "官方", "相关", "今日", "最新", "热搜"}:
                continue
            c[w] = c.get(w, 0) + 1
    top = sorted(c.items(), key=lambda x: -x[1])[:limit]
    return [{"word": w, "count": n} for w, n in top]


def _assistant(q):
    q = q or ""
    focus = None
    for kw in ["AI", "大模型", "苹果", "iPhone", "小米", "华为", "汽车", "新能源", "比亚迪", "特斯拉"]:
        if kw.lower() in q.lower():
            focus = kw
            break
    if not focus:
        theme = next((t for t in THEMES if any(k.lower() in q.lower() for k in t["keywords"])), None)
        if theme:
            focus = theme["keywords"][0]
    matched = [r for r in ARTICLES if focus and focus.lower() in (r.get("title") or "").lower()]
    if not matched:
        return f"<p>当前数据中未找到与「{q[:20]}」相关的内容。可更换关键词后重试。</p>"
    n = len(matched)
    e = {"正面": 0, "中性": 0, "负面": 0}
    for r in matched:
        e[r.get("sentiment_label", "中性")] = e.get(r.get("sentiment_label", "中性"), 0) + 1
    negs = [r for r in matched if r.get("sentiment_label") == "负面"][:3]
    pct = round(e["负面"] / n * 100, 1)
    tone = "负面占比偏高，建议重点关注" if pct >= 30 else ("整体可控，关注个别负面" if pct >= 15 else "整体平稳")
    lines = [f"<p>共匹配 <strong>{n}</strong> 条相关舆情，情感分布：正面 {e['正面']} / 中性 {e['中性']} / 负面 {e['负面']}（负面占比 {pct}%）。{tone}。</p>"]
    if negs:
        lines.append("<p>主要负面点：</p><ul>")
        for r in negs:
            lines.append(f"<li>{r['title']} <span style='color:#a33b3b'>({SRC_NAME.get(r['source'], r['source'])})</span></li>")
        lines.append("</ul>")
    lines.append("<p><b>建议口径：</b>正视问题、快速核查事实、及时向公众同步进展，并保留原文链接备查。</p>")
    # 可选：接入 LLM 做「情感与态度」归纳（态度强度/立场/情感迁移/理由）
    if _insight and os.environ.get("LLM_API_KEY") and matched:
        try:
            feed = [{
                "title": r.get("title"),
                "content": r.get("content"),
                "publish_time": str(r.get("publish_time") or ""),
                "sentiment": {"label": r.get("sentiment_label", "中性")},
            } for r in matched]
            res = _insight.summarize_emotion_attitude(feed)
            emo = (res or {}).get("emotion") or {}
            if emo.get("emotion_shift"):
                stance = emo.get("stance_split") or {}
                stance_txt = "、".join(f"{k}{v}%" for k, v in stance.items()) if stance else ""
                lines.append(
                    f"<p><b>AI 情感归纳：</b>主导情绪 {emo.get('dominant_emotion') or '无'}；"
                    f"情感迁移：{emo['emotion_shift']}。"
                    + (f"立场分化：{stance_txt}。" if stance_txt else "")
                    + "<span style='color:#a33b3b'>（AI 归纳，仅供复核参考）</span></p>"
                )
        except Exception:
            pass
    return "".join(lines)


def _insight_emotion():
    """调用 LLM 对当前文章做「情感与态度」归纳（态度强度/立场/情感迁移/理由）。"""
    if not _insight:
        return {"available": False, "error": "insight_llm 模块未加载"}
    if not os.environ.get("LLM_API_KEY"):
        return {"available": False, "error": "未配置大模型，请在设置页填写 LLM 的 API Key"}
    rows = sorted(ARTICLES, key=lambda r: r.get("publish_time") or dt.datetime.min)
    # 分层采样：负面/正面/中性各取最近的若干篇，保证归纳覆盖不同情感（舆情关注负面，权重略高）
    neg = [r for r in rows if r.get("sentiment_label") == "负面"]
    pos = [r for r in rows if r.get("sentiment_label") == "正面"]
    neu = [r for r in rows if r.get("sentiment_label") == "中性"]
    sample = (neg[-12:] + pos[-8:] + neu[-12:])
    sample.sort(key=lambda r: r.get("publish_time") or dt.datetime.min)
    feed = [{
        "title": r.get("title"),
        "content": r.get("content"),
        "publish_time": str(r.get("publish_time") or ""),
        "sentiment": {"label": r.get("sentiment_label", "中性")},
    } for r in sample]
    res = _insight.summarize_emotion_attitude(feed, retries=2)
    # 打印到服务端控制台，便于排查（成功/失败 + 具体原因）
    if res.get("emotion"):
        print(f"[insight] 归纳成功：主导情绪={res['emotion'].get('dominant_emotion')} 置信度={res['emotion'].get('confidence')}")
    else:
        print(f"[insight] 归纳失败：{res.get('error')}")
    return res


def _sample_feed_from_rows(rows):
    """分层采样：负面/正面/中性各取若干，带上来源供模型引用。"""
    ordered = sorted(rows, key=lambda r: r.get("publish_time") or dt.datetime.min)
    neg = [r for r in ordered if r.get("sentiment_label") == "负面"]
    pos = [r for r in ordered if r.get("sentiment_label") == "正面"]
    neu = [r for r in ordered if r.get("sentiment_label") == "中性"]
    sample = (neg[-12:] + pos[-8:] + neu[-12:])
    sample.sort(key=lambda r: r.get("publish_time") or dt.datetime.min)
    return [{
        "title": r.get("title"),
        "content": r.get("content"),
        "publish_time": str(r.get("publish_time") or ""),
        "source": SRC_NAME.get(r.get("source"), r.get("source") or ""),
        "sentiment": {"label": r.get("sentiment_label", "中性")},
    } for r in sample]


def _metrics_text(scope=None):
    m = _metrics(scope)
    emo = m["emotion"]
    return (
        f"舆情总量 {m['total']} 条；"
        f"情感分布：正面 {emo['pos']} 条（{emo['pos_ratio']}%）、中性 {emo['neu']} 条（{emo['neu_ratio']}%）、"
        f"负面 {emo['neg']} 条（{emo['neg_ratio']}%）；"
        f"来源构成：" + "、".join(f"{s['source']} {s['count']} 条" for s in m["source"]) + "；"
        f"近7天声量：" + "、".join(f"{t['date']} {t['count']} 条" for t in m["trend"]) + "。"
    )


def _insight_sixdim(scope=None):
    """调用 LLM 对当前范围（主题/事件）文章做「六维舆情分析」。"""
    if not _insight:
        return {"available": False, "error": "insight_llm 模块未加载"}
    if not os.environ.get("LLM_API_KEY"):
        return {"available": False, "error": "未配置大模型，请在设置页填写 LLM 的 API Key"}
    scoped = _scope_rows(scope)
    if not scoped:
        return {"available": False, "error": "当前主题/事件下暂无舆情数据，无法生成六维分析"}
    feed = _sample_feed_from_rows(scoped)
    res = _insight.summarize_sixdimensions(feed, retries=2)
    if res.get("six_dimensions"):
        print("[insight-sixdim] 六维分析成功")
    else:
        print(f"[insight-sixdim] 六维分析失败：{res.get('error')}")
    return res


def _gen_report(scope=None):
    """生成《舆情研判分析报告》（8 段式模板，Markdown）。"""
    if not _insight:
        return {"available": False, "error": "insight_llm 模块未加载"}
    if not os.environ.get("LLM_API_KEY"):
        return {"available": False, "error": "未配置大模型，请在设置页填写 LLM 的 API Key"}
    scoped = _scope_rows(scope)
    if not scoped:
        return {"available": False, "error": "当前主题/事件下暂无舆情数据，无法生成报告"}
    metrics_text = _metrics_text(scope)
    feed = _sample_feed_from_rows(scoped)
    res = _insight.generate_report(metrics_text, feed, retries=1)
    if res.get("report"):
        print("[report] 报告生成成功")
    else:
        print(f"[report] 报告生成失败：{res.get('error')}")
    return res


def _plan_route(scope=None, sixdim=None, topic_name=""):
    """规则推荐应对方案模板（不调用大模型）。"""
    if not _insight:
        return {"available": False, "error": "insight_llm 模块未加载"}
    if not sixdim:
        return {"available": False, "error": "请先生成六维分析"}
    metrics = _metrics(scope)
    route = _insight.route_plan_template(metrics, sixdim, scope=scope or "", topic_name=topic_name or "")
    route["available"] = True
    route["hint"] = (
        "三种模板都是应对方案（分析约三成）。当前不像突发；要写战时处置请改选危机模板。详细数据请用研判报告。"
        if route.get("template_id") != "crisis"
        else "危机应对方案须确认后才生成，结果须人工复核后对外使用"
    )
    return route


def _gen_plan(scope=None, template_id="", sixdim=None, topic_name=""):
    """按用户确认的模板生成应对方案/分型报告。"""
    if not _insight:
        return {"available": False, "error": "insight_llm 模块未加载"}
    if not os.environ.get("LLM_API_KEY"):
        return {"available": False, "error": "未配置大模型，请在设置页填写 LLM 的 API Key"}
    if not sixdim:
        return {"available": False, "error": "请先生成六维分析"}
    scoped = _scope_rows(scope)
    if not scoped:
        return {"available": False, "error": "当前主题/事件下暂无舆情数据，无法生成方案"}
    feed = _sample_feed_from_rows(scoped)
    res = _insight.generate_plan(
        template_id, _metrics_text(scope), sixdim, feed,
        topic_name=topic_name or "", retries=1,
    )
    if res.get("plan"):
        print(f"[plan] 方案生成成功 template={template_id}")
    else:
        print(f"[plan] 方案生成失败：{res.get('error')}")
    return res


def _collect_refresh():
    """采集新文章 → 清洗 → 情感打标 → 合并去重 → 写回 articles.json（数据可实时更新）。"""
    if not _collect or not _clean:
        return {"ok": False, "error": "采集模块未加载"}
    try:
        items, summary = _collect.collect(limit_per_source=60)
        items, stats = _clean.clean(items)
        existing = {r.get("url") for r in ARTICLES if r.get("url")}
        max_id = max([int(r.get("id", 0)) for r in ARTICLES] or [0])
        new_count = 0
        for it in items:
            if not it.get("url") or it["url"] in existing:
                continue
            text = (it.get("title") or "") + "。" + (it.get("content") or "")
            if _sentiment:
                try:
                    senti = _sentiment.analyze(text)["sentiment"]
                    it["sentiment_label"] = senti["label"]
                    it["sentiment_score"] = senti["score"]
                    it["sentiment_confidence"] = senti["confidence"]
                except Exception:
                    it.setdefault("sentiment_label", "中性")
            else:
                it.setdefault("sentiment_label", "中性")
            it.setdefault("read_count", 0)
            it.setdefault("comment_count", 0)
            it.setdefault("forward_count", 0)
            it["publish_time"] = it.get("publish_time") or dt.datetime.now()
            max_id += 1
            it["id"] = max_id
            ARTICLES.append(it)
            existing.add(it["url"])
            new_count += 1
        # 写回 articles.json（持久化，datetime 用 default=str 序列化）
        try:
            with open(DATA_FILE, "w", encoding="utf-8-sig") as f:
                json.dump(ARTICLES, f, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"[collect] 写回 articles.json 失败：{e}")
        print(f"[collect] 采集完成：新增 {new_count} 条，共 {len(ARTICLES)} 条")
        return {"ok": True, "summary": summary, "stats": stats,
                "new_count": new_count, "total": len(ARTICLES)}
    except Exception as e:
        print(f"[collect] 采集异常：{e}")
        return {"ok": False, "error": str(e)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=_HERE, **kw)

    def end_headers(self):
        # 原型开发：禁止浏览器缓存 JS/CSS/HTML，避免改完前端不生效
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, params = parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/api/overview":
                return self._json({"code": 0, "data": _overview()})
            if path == "/api/topics":
                return self._json({"code": 0, "data": _topics()})
            if path == "/api/articles":
                return self._json({"code": 0, "data": _articles(
                    params.get("topic"), params.get("media"), params.get("sentiment"),
                    params.get("q"), int(params.get("limit", 200)))})
            if path == "/api/hot":
                return self._json({"code": 0, "data": _hot()})
            if path == "/api/metrics":
                return self._json({"code": 0, "data": _metrics(params.get("scope"))})
            if path == "/api/config":
                return self._json({"code": 0, "data": _public_config(_load_config_file())})
            if path == "/api/sentiment-result":
                return self._json({"code": 0, "data": _sentiment_snapshot()})
            if path == "/api/assistant":
                return self._json({"code": 0, "data": {"html": _assistant(params.get("q", ""))}})
        except Exception as e:
            return self._json({"code": 500, "msg": str(e)}, 500)
        return super().do_GET()

    def do_POST(self):
        try:
            if urlparse(self.path).path == "/api/collect":
                return self._json({"code": 0, "data": _collect_refresh()})
            if urlparse(self.path).path == "/api/recompute-sentiment":
                return self._json({"code": 0, "data": _recompute()})
            if urlparse(self.path).path == "/api/insight-emotion":
                return self._json({"code": 0, "data": _insight_emotion()})
            if urlparse(self.path).path == "/api/insight-sixdim":
                qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                return self._json({"code": 0, "data": _insight_sixdim(qs.get("scope"))})
            if urlparse(self.path).path == "/api/report":
                qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                return self._json({"code": 0, "data": _gen_report(qs.get("scope"))})
            if urlparse(self.path).path == "/api/plan-route":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or "{}")
                return self._json({"code": 0, "data": _plan_route(
                    body.get("scope"), body.get("six_dimensions"), body.get("topic_name") or "")})
            if urlparse(self.path).path == "/api/plan":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or "{}")
                return self._json({"code": 0, "data": _gen_plan(
                    body.get("scope"), body.get("template_id") or "",
                    body.get("six_dimensions"), body.get("topic_name") or "")})
            if urlparse(self.path).path == "/api/config":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or "{}")
                cfg = _save_config(body)
                return self._json({"code": 0, "data": _public_config(cfg)})
            if urlparse(self.path).path == "/api/themes":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or "{}")
                saved = _save_theme(body)
                if not saved.get("ok"):
                    return self._json({"code": 1, "data": saved, "msg": saved.get("error") or "保存失败"})
                return self._json({"code": 0, "data": saved})
            return self._json({"code": 404, "msg": "not found"}, 404)
        except Exception as e:
            print(f"[server] POST 处理异常：{e}")
            return self._json({"code": 500, "data": {"six_dimensions": None, "error": f"服务异常：{e}"}}, 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
    _load_config_file()  # 先加载页面保存的 API 配置，让情感引擎/LLM 生效
    _load()
    print(f"PRD 原型数据服务: http://127.0.0.1:{port}/  (Ctrl+C 停止)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
