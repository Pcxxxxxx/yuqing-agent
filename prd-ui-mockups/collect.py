# -*- coding: utf-8 -*-
"""
collect 技能（P0）· 实现
对应 技能接口规范 v0.2 3.1 节：
  入参 source / keywords / time_range / limit / dedup
  出参 items[] / next_cursor / source_summary

数据源（本机可达、合规免费）：
  - it_home   IT之家 RSS  (科技/数码)
  - sspai     少数派 RSS  (效率/科技生活)
  - kr36      36氪 RSS    (商业/创投)
  - baidu_hot 百度热搜 API (全网热点话题)
"""
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import json
import hashlib
import datetime as dt
import re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

SOURCES = {
    "it_home": {"type": "rss", "url": "https://www.ithome.com/rss/", "author": "IT之家"},
    "sspai":   {"type": "rss", "url": "https://sspai.com/feed", "author": "少数派"},
    # kr36 有 WAF 反爬，返回 HTML 而非 RSS，已停用
    "baidu_hot": {"type": "json", "url": "https://top.baidu.com/api/board?platform=wise&tab=realtime", "author": "百度热搜"},
    "ifanr":    {"type": "rss", "url": "https://www.ifanr.com/feed", "author": "爱范儿"},
    "geekpark": {"type": "rss", "url": "https://www.geekpark.net/rss", "author": "极客公园"},
    "leiphone": {"type": "rss", "url": "https://www.leiphone.com/feed", "author": "雷锋网"},
    "weibo_hot": {"type": "weibo", "url": "https://weibo.com/ajax/side/hotSearch", "author": "微博热搜",
                  "headers": {"Referer": "https://weibo.com/", "Accept": "application/json"}},
    "zhihu_hot": {"type": "zhihu", "url": "https://api.zhihu.com/topstory/hot-list", "author": "知乎热榜",
                  "headers": {"Referer": "https://www.zhihu.com/", "Accept": "application/json"}},
}


def _fetch(url, timeout=15, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        charset = r.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="ignore")


def _strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _parse_rss(source_key, xml_text):
    items = []
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    node = channel if channel is not None else root
    for item in node.findall("item"):
        title = _strip_html((item.findtext("title") or ""))
        link = (item.findtext("link") or "").strip()
        desc = _strip_html(item.findtext("description") or "")
        pub = (item.findtext("pubDate") or "").strip()
        author = (item.findtext("author") or SOURCES[source_key]["author"]).strip()
        if not title or not link:
            continue
        # 解析日期（处理 GMT/UTC 等时区名）
        try:
            t = dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
            publish = t.astimezone().replace(tzinfo=None)
        except Exception:
            try:
                for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S"):
                    t = dt.datetime.strptime(pub, fmt)
                    publish = t
                    break
            except Exception:
                publish = dt.datetime.now()
        items.append({
            "source": source_key,
            "author": author,
            "title": title,
            "content": desc,
            "url": link,
            "publish_time": publish,
        })
    return items


def _parse_baidu_hot(json_text):
    items = []
    data = json.loads(json_text)
    cards = data.get("data", {}).get("cards", [])
    for card in cards:
        for block in card.get("content", []):
            for item in block.get("content", []):
                word = item.get("word")
                url = item.get("url")
                if not word:
                    continue
                items.append({
                    "source": "baidu_hot",
                    "author": "百度热搜",
                    "title": f"【热搜】{word}",
                    "content": f"百度实时热搜话题：{word}（index={item.get('index')}）",
                    "url": url or "https://top.baidu.com",
                    "publish_time": dt.datetime.now(),
                })
    return items


def _parse_weibo_hot(json_text):
    items = []
    data = json.loads(json_text)
    realtime = (data.get("data") or {}).get("realtime") or []
    for it in realtime:
        word = it.get("word") or it.get("note")
        num = it.get("num") or it.get("raw_hot")
        if not word:
            continue
        items.append({
            "source": "weibo_hot",
            "author": "微博热搜",
            "title": f"【热搜】{word}",
            "content": f"微博实时热搜话题：{word}（热度 {num}）",
            "url": "https://s.weibo.com/weibo?q=" + urllib.parse.quote(word),
            "publish_time": dt.datetime.now(),
        })
    return items


def _parse_zhihu_hot(json_text):
    items = []
    data = json.loads(json_text)
    hot_list = data.get("data") or []
    for it in hot_list:
        target = it.get("target") or {}
        title = target.get("title") or it.get("target_title")
        if not title:
            continue
        # 知乎返回的 url 是 api 地址，需转成网页地址
        url = target.get("url") or ""
        tid = target.get("id")
        if "api.zhihu.com/questions/" in url and tid:
            url = f"https://www.zhihu.com/question/{tid}"
        elif not url.startswith("https://www.zhihu.com/"):
            url = "https://www.zhihu.com/hot"
        items.append({
            "source": "zhihu_hot",
            "author": "知乎热榜",
            "title": f"【热榜】{title}",
            "content": f"知乎实时热榜话题：{title}",
            "url": url,
            "publish_time": dt.datetime.now(),
        })
    return items


def collect(sources=None, limit_per_source=100, time_range=None):
    """collect 技能主体。返回 items 列表（含 source_summary 汇总）。"""
    sources = sources or list(SOURCES.keys())
    all_items, summary = [], {}
    for key in sources:
        cfg = SOURCES.get(key)
        if not cfg:
            continue
        try:
            raw = _fetch(cfg["url"], headers=cfg.get("headers"))
            if cfg["type"] == "rss":
                items = _parse_rss(key, raw)
            elif cfg["type"] == "weibo":
                items = _parse_weibo_hot(raw)
            elif cfg["type"] == "zhihu":
                items = _parse_zhihu_hot(raw)
            else:
                items = _parse_baidu_hot(raw)
            summary[key] = len(items)
            all_items.extend(items[:limit_per_source])
        except Exception as e:
            summary[key] = f"error:{e.__class__.__name__}"
    # 时间过滤
    if time_range:
        start = time_range.get("start")
        end = time_range.get("end")
        all_items = [i for i in all_items
                     if (not start or i["publish_time"] >= start)
                     and (not end or i["publish_time"] <= end)]
    # 生成源内主键
    for it in all_items:
        it["item_id"] = hashlib.md5(f"{it['source']}|{it['url']}".encode()).hexdigest()[:16]
    return all_items, summary


if __name__ == "__main__":
    items, summary = collect(limit_per_source=50)
    print("source_summary:", summary)
    print("共采集:", len(items), "条")
    for it in items[:5]:
        print(f"  [{it['source']}] {it['title'][:40]}  {it['publish_time']}")
