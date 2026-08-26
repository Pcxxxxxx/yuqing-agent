# -*- coding: utf-8 -*-
"""
clean 技能（P0 支撑）· 实现
对应 技能接口规范 v0.2 3.2 节：
  去重(dedup) / 去垃圾(garbage) / 规整格式
  出参 items[] / stats{input_count, removed_count, duplicate_count, kept_count}
"""
import re
import hashlib


def _normalize(text):
    return re.sub(r"\s+", "", text or "")


def _is_garbage(item):
    t = _normalize(item.get("title", ""))
    c = _normalize(item.get("content", ""))
    combined = t + c
    if len(combined) < 10:          # 过短
        return True
    if re.search(r"(纯属虚构|仅供娱乐|广告|推广|点击领取|限时优惠|加微信|加QQ|\
                  vip|会员开通|关注公众号)", combined, re.I):   # 广告/垃圾
        return True
    return False


def clean(items, min_length=10):
    """清洗：去垃圾 → 去重 → 规整。返回 (kept_items, stats)。"""
    stats = {"input_count": len(items), "removed_count": 0, "duplicate_count": 0}
    kept = []
    seen = set()
    for it in items:
        if _is_garbage(it):
            stats["removed_count"] += 1
            continue
        content_hash = hashlib.md5(_normalize(it.get("title") or "").encode()).hexdigest()
        if content_hash in seen:
            stats["duplicate_count"] += 1
            continue
        seen.add(content_hash)
        # 规整
        it["content"] = (it.get("content") or "").strip()[:5000]
        it["title"] = (it.get("title") or "").strip()[:512]
        it["clean_hash"] = content_hash
        kept.append(it)
    stats["kept_count"] = len(kept)
    return kept, stats


if __name__ == "__main__":
    # 简单自测
    test = [
        {"title": "真实新闻标题测试", "content": "这是一条正常的新闻内容用于测试清洗逻辑是否正常"},
        {"title": "广告：限时优惠", "content": "加微信领取优惠"},
        {"title": "真实新闻标题测试", "content": "重复内容"},
        {"title": "短", "content": "x"},
    ]
    kept, stats = clean(test)
    print("stats:", stats)
    for k in kept:
        print(" 保留:", k["title"])
