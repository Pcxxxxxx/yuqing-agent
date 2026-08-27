# -*- coding: utf-8 -*-
"""
sentiment_analyze 技能（P0 核心）· 可插拔 provider 实现
对应 技能接口规范 §3.3：输出 {label, label_code, score, confidence}
对齐 PRD(3) 第五章「情感标注策略」：模型自动打标（正/中/负 + confidence）+ 人工校验。

Provider 可插拔（环境变量 SENTIMENT_PROVIDER）：
  - rule    规则词典（免费、离线，demo / 无 key 降级用）——当前默认
  - aliyun  阿里云 NLP 情感分析 GetSaChGeneral（专用情感 API）
  - xfyun   讯飞开放平台情感分析（专用情感 API）
  - selfhosted  私有化 RoBERTa/BERT 服务（自建 HTTP JSON，最合规，推荐政企）

切换示例（Windows PowerShell）：
  $env:SENTIMENT_PROVIDER = "aliyun"
  $env:ALIYUN_AK_ID      = "LTAI..."
  $env:ALIYUN_AK_SECRET  = "..."
  py pipeline.py

设计约定（对齐 PRD）：
  - 未配置 key / 调用失败 / 超时：降级到 rule，并在 model_meta.degraded 标记 true（供审计与告警）
  - score ∈ [-1, 1]：由 API 概率映射 score = positive_prob - negative_prob（规则 provider 沿用词典分）
  - confidence ∈ [0, 1]：直接用 API 返回概率/置信度，低置信度(<0.6)建议走人工抽检改标留痕
  - 细分情绪(愤怒/恐惧/悲伤…)、态度强度、立场、情感迁移、理由：不在此技能，归 insight_llm（LLM 归纳）
"""
import os
import re
import time
import uuid
import json
import hmac
import base64
import hashlib
import urllib.request
import urllib.parse

# ============================================================
# 一、rule provider（规则词典，保留作离线降级与 demo）
# ============================================================
POSITIVE_WORDS = {
    "好评", "点赞", "支持", "上涨", "增长", "突破", "创新", "领先", "夺冠", "获奖",
    "成功", "顺利", "满意", "感谢", "推荐", "优秀", "出色", "惊艳", "强劲", "热卖", "爆款",
    "利好", "改善", "提升", "增强", "加速", "降息", "减税", "盈利", "增持", "回购",
    "回暖", "复苏", "乐观", "看好", "给力", "牛", "好评如潮",
    "发展", "进步", "超越", "第一", "新高", "里程碑", "实现突破", "交付", "量产", "签约",
    "合作", "共赢", "保障", "惠民", "便民", "幸福", "暖心", "致敬", "英雄",
}

NEGATIVE_WORDS = {
    "暴跌", "下滑", "亏损", "违规", "处罚", "罚款", "召回", "投诉", "翻车", "造假", "欺诈",
    "侵权", "泄露", "黑客", "漏洞", "崩溃", "宕机", "延迟", "缺陷", "质量问题", "不合格",
    "停运", "停产", "裁员", "降薪", "欠薪", "破产", "倒闭", "关门", "跑路", "退市", "下架",
    "封禁", "审查", "事故", "伤亡", "遇难", "起火", "爆炸", "泄漏", "污染", "超标", "疾病",
    "疫情", "感染", "死亡", "纠纷", "诉讼", "判决", "逮捕", "拘留", "贪污", "腐败", "传唤",
    "批评", "质疑", "差评", "糟糕", "失望", "愤怒", "担忧", "风险", "危机", "恶化",
    "疲软", "冷清", "缩水", "萎缩", "取消", "终止", "抵制", "抗议", "罢工", "涨价",
    "拥堵", "排队", "混乱", "骗局", "陷阱", "套路", "割韭菜", "暴雷", "踩雷", "崩盘",
}

NEGATION_WORDS = {"不", "没", "无", "未", "非", "别", "莫", "勿"}


def _rule_score(text: str) -> float:
    """粗粒度情感分数：-1~1"""
    pos = sum(1 for w in POSITIVE_WORDS if w in text)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text)
    if pos == 0 and neg == 0:
        return 0.0
    raw = (pos - neg) / (pos + neg)
    has_neg = any(w in text for w in NEGATION_WORDS)
    if has_neg and raw != 0:
        raw = -raw * 0.8
    return max(-1.0, min(1.0, raw))


def _rule_confidence(text: str) -> float:
    word_len = max(1, len(re.findall(r"[\u4e00-\u9fff]{1}", text)))
    hits = sum(1 for w in POSITIVE_WORDS | NEGATIVE_WORDS if w in text)
    return round(max(0.2, min(1.0, hits / word_len * 10)), 2)


def _rule_analyze(text: str):
    return {"score": _rule_score(text), "confidence": _rule_confidence(text)}


# ============================================================
# 二、label 映射（统一由 score 决定，与 provider 解耦）
# ============================================================
def _to_label(score: float, dimension: str, neutral_threshold: float):
    """把 score∈[-1,1] 映射为 label/label_code。"""
    if dimension == "5":
        if score >= 0.6:
            return "强烈正向", "SPOS"
        if score >= neutral_threshold:
            return "正向", "POS"
        if score <= -0.6:
            return "强烈负向", "SNEG"
        if score <= -neutral_threshold:
            return "负向", "NEG"
        return "中性", "NEU"
    if score >= neutral_threshold:
        return "正面", "POS"
    if score <= -neutral_threshold:
        return "负面", "NEG"
    return "中性", "NEU"


# ============================================================
# 三、专用情感 API provider
# ============================================================
_TIMEOUT = 10


def _percent_encode(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~")


def _aliyun_rpc_sign(secret: str, method: str, params: dict) -> str:
    """阿里云 POP RPC 签名（SignatureVersion=1.0，HMAC-SHA1）。"""
    canonical = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = f"{method}&%2F&{_percent_encode(canonical)}"
    return base64.b64encode(
        hmac.new((secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")


def _call_aliyun(text: str):
    """阿里云 NLP 情感分析 GetSaChGeneral。返回 {score, confidence}。"""
    ak_id = os.environ.get("ALIYUN_AK_ID")
    ak_secret = os.environ.get("ALIYUN_AK_SECRET")
    if not ak_id or not ak_secret:
        raise RuntimeError("aliyun provider 未配置 ALIYUN_AK_ID / ALIYUN_AK_SECRET")

    params = {
        "Action": "GetSaChGeneral",
        "Version": "2018-04-08",
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "AccessKeyId": ak_id,
        "Text": text[:5000],
        # "ServiceCode": "general",  # 通用领域；可按需换 "ecommerce" 等
    }
    params["Signature"] = _aliyun_rpc_sign(ak_secret, "GET", params)
    url = "https://nlp.cn-hangzhou.aliyuncs.com/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        body = json.loads(r.read().decode("utf-8"))
    # GetSaChGeneral 返回 data.result: {sentiment, positive_prob, neutral_prob, negative_prob}
    result = (body.get("data") or {}).get("result") or {}
    pos = float(result.get("positive_prob", 0))
    neu = float(result.get("neutral_prob", 0))
    neg = float(result.get("negative_prob", 0))
    confidence = round(max(pos, neu, neg), 4)  # 取最大概率作为 confidence
    score = round(pos - neg, 4)
    return {"score": score, "confidence": confidence}


def _call_xfyun(text: str):
    """讯飞开放平台情感分析（ltpapi，MD5 校验和）。返回 {score, confidence}。"""
    appid = os.environ.get("XFYUN_APPID")
    apikey = os.environ.get("XFYUN_APIKEY")
    if not appid or not apikey:
        raise RuntimeError("xfyun provider 未配置 XFYUN_APPID / XFYUN_APIKEY")

    cur_time = str(int(time.time()))
    param = base64.b64encode(json.dumps({"type": "dependent"}).encode("utf-8")).decode("utf-8")
    checksum = hashlib.md5((apikey + cur_time + param).encode("utf-8")).hexdigest()
    headers = {
        "X-Appid": appid,
        "X-CurTime": cur_time,
        "X-Param": param,
        "X-CheckSum": checksum,
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }
    data = urllib.parse.urlencode({"text": text[:5000]}).encode("utf-8")
    req = urllib.request.Request("https://ltpapi.xfyun.cn/v2/sa", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        body = json.loads(r.read().decode("utf-8"))
    if str(body.get("code", "0")) != "0":
        raise RuntimeError(f"xfyun 返回错误: {body.get('message') or body.get('desc')}")

    d = body.get("data") or {}
    pos = float(d.get("positive_prob", 0))
    neu = float(d.get("neutral_prob", 0))
    neg = float(d.get("negative_prob", 0))
    # 部分版本只回 sentiment(0/1/2) + confidence，做字段兼容
    if pos == 0 and neg == 0 and neu == 0:
        s = int(d.get("sentiment", 1))
        pos, neu, neg = {(1,): (1, 0, 0), (2,): (0, 1, 0), (0,): (0, 0, 1)}.get(
            (s,), (0, 1, 0))
    conf = d.get("confidence")
    confidence = round(float(conf) if conf is not None else max(pos, neu, neg), 4)
    score = round(pos - neg, 4)
    return {"score": score, "confidence": confidence}


def _call_selfhosted(text: str):
    """私有化 RoBERTa/BERT 服务（自建 HTTP JSON）。返回 {score, confidence}。"""
    base = os.environ.get("SENTIMENT_SELFHOSTED_URL")
    if not base:
        raise RuntimeError("selfhosted provider 未配置 SENTIMENT_SELFHOSTED_URL")
    data = json.dumps({"text": text[:5000]}).encode("utf-8")
    req = urllib.request.Request(
        base, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        body = json.loads(r.read().decode("utf-8"))
    # 约定服务返回 {positive_prob, neutral_prob, negative_prob} 或 {score, confidence}
    if "score" in body:
        return {"score": float(body["score"]), "confidence": float(body.get("confidence", 0.6))}
    pos = float(body.get("positive_prob", 0))
    neu = float(body.get("neutral_prob", 0))
    neg = float(body.get("negative_prob", 0))
    return {"score": round(pos - neg, 4), "confidence": round(max(pos, neu, neg), 4)}


_PROVIDERS = {
    "rule": _rule_analyze,
    "aliyun": _call_aliyun,
    "xfyun": _call_xfyun,
    "selfhosted": _call_selfhosted,
}


# ============================================================
# 四、技能入口（保持与 pipeline.py 的调用契约一致）
# ============================================================
def analyze(text: str, dimension: str = "3", neutral_threshold: float = 0.1):
    """实现 sentiment_analyze 接口。

    返回: {"sentiment": {label, label_code, score, confidence},
           "model_meta": {provider, degraded, low_confidence_review}}
    """
    if not text:
        return {
            "sentiment": {"label": "中性", "label_code": "NEU", "score": 0.0, "confidence": 0.0},
            "model_meta": {"provider": "empty", "degraded": False, "low_confidence_review": False},
        }

    text = re.sub(r"<[^>]+>", "", text)  # 去 HTML

    provider_name = (os.environ.get("SENTIMENT_PROVIDER") or "rule").strip().lower()
    if provider_name not in _PROVIDERS:
        provider_name = "rule"
    provider = _PROVIDERS[provider_name]

    degraded = False
    try:
        raw = provider(text)
    except Exception:
        # 降级：API 未配置 / 失败 / 超时，回落到规则词典，保证管线不中断
        raw = _rule_analyze(text)
        degraded = True

    score = max(-1.0, min(1.0, raw["score"]))
    confidence = round(max(0.0, min(1.0, raw["confidence"])), 2)
    label, code = _to_label(score, dimension, neutral_threshold)

    return {
        "sentiment": {
            "label": label,
            "label_code": code,
            "score": round(score, 2),
            "confidence": confidence,
        },
        "model_meta": {
            "provider": provider_name,
            "degraded": degraded,
            "low_confidence_review": confidence < 0.6,  # 触发 PRD 人工抽检改标留痕
        },
    }


def analyze_batch(texts, dimension="3"):
    """批量入口，供 pipeline / 主控调用。"""
    return [analyze(t, dimension) for t in texts]


if __name__ == "__main__":
    print("当前 provider:", os.environ.get("SENTIMENT_PROVIDER") or "rule")
    samples = [
        "公司业绩大幅增长，实现历史新高的突破，投资者纷纷看好",
        "产品出现严重质量问题，大量用户投诉并引发召回",
        "今天天气不错，股市平稳运行",
        "官方辟谣：网传消息不实，请不要相信谣言",
    ]
    for s in samples:
        r = analyze(s)
        print(s, "=>", r["sentiment"], r["model_meta"])
