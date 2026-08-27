# -*- coding: utf-8 -*-
"""
insight_analyze 技能 · 「情感与态度」维度的 LLM 归纳实现（对齐 PRD(3) 六维分析）
对应 技能接口规范 §3.10：态度强度 / 立场分化 / 情感迁移 / 理由 等 AI 归纳项。

职责边界（对齐 PRD 第五章）：
  - 情感性质（正/中/负）       → sentiment_analyze（专用情感 API，见 sentiment.py）
  - 细分情绪 / 态度强度 / 立场  → 本模块单条标注 annotate_emotion()
  - 立场分化 / 情感迁移         → 本模块主题级归纳 summarize_emotion_attitude()
  - AI 归纳块须附「人工复核提示」：本模块输出里带 review_flag=True

LLM 配置（OpenAI 兼容协议，适配私有化 vLLM / 通义 / 星火 / GPT 等）：
  $env:LLM_BASE_URL = "https://api.openai.com/v1"   # 或私有化地址
  $env:LLM_API_KEY  = "sk-..."
  $env:LLM_MODEL    = "gpt-4o-mini"                 # 按实际填

未配置 LLM_API_KEY 时，本模块不联网、不报错，返回空归纳 + review_flag，
由主控在缺数据时提示「暂无足够数据」（对齐 PRD 禁止编造）。
"""
import os
import re
import json
import urllib.request

_TIMEOUT = 180


def _llm_env():
    """每次调用时读环境变量，便于「设置页保存后」即时生效（server.py 会 os.environ.update）。"""
    return (
        os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        os.environ.get("LLM_API_KEY", ""),
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    )

# 单条文本的情感标注 Prompt（沿用「舆情情感分析师」思路，输出强约束 JSON）
PROMPT_ANNOTATE = """你是一位专业的舆情情感分析师。请对以下文本进行情感标注。

【文本】：{text}

请输出以下维度，仅输出一个 JSON 对象，不要输出任何解释、不要用 Markdown 代码块包裹：
1. sentiment_polarity：情感极性，取值 正面/中性/负面
2. emotion：细分情绪，取值 愤怒/恐惧/悲伤/嘲讽/幸灾乐祸/感恩/自豪/无
3. attitude_strength：态度强度，1-5 的整数，5 为最激烈
4. stance：立场倾向，取值 支持/反对/中立/理中客
5. confidence：置信度，0-1 的小数
6. reason：理由，50 字以内

输出格式（严格按此结构）：
{{"sentiment_polarity":"负面","emotion":"愤怒","attitude_strength":4,"stance":"反对","confidence":0.9,"reason":"……"}}"""

# 主题/事件级「情感与态度」归纳 Prompt（输入已打情感标签的条目，输出六维 emotion 子维度）
PROMPT_SUMMARIZE = """你是一位专业的舆情情感分析师。以下是某主题/事件下的一批舆情条目（已带机器情感标签与时间）。

【条目列表】：
{items_text}

请基于上述真实条目，输出该主题/事件的「情感与态度」归纳，仅输出一个 JSON 对象，不要输出任何解释：
1. attitude_strength：态度强度分布，如 {{"激烈":20,"中等":45,"温和":35}}（百分比，和为 100）
2. stance_split：立场分化，如 {{"支持":10,"反对":60,"中立":25,"理中客":5}}（百分比，和为 100）
3. emotion_shift：情感迁移，按时间概括情感变化趋势，一句话（如"由质疑转向愤怒，召回后负面情绪升级"）
4. dominant_emotion：主导细分情绪，取值 愤怒/恐惧/悲伤/嘲讽/幸灾乐祸/感恩/自豪/无
5. reasons：关键归因（基于原文，最多 3 条，每条 30 字内）
6. confidence：归纳置信度，0-1 的小数

约束：只能基于给定条目归纳，禁止编造条目中不存在的信息；证据不足时对应字段写「暂无足够数据」。

输出格式（严格按此结构）：
{{"attitude_strength":{{"激烈":0,"中等":0,"温和":0}},"stance_split":{{"支持":0,"反对":0,"中立":0,"理中客":0}},"emotion_shift":"","dominant_emotion":"无","reasons":[],"confidence":0.0}}"""

# 六维舆情分析 Prompt（对齐 PRD(4) 六维：基础/情感/叙事/传播/行为/深层）
PROMPT_SIXDIM = """你是一位专业的舆情分析师。以下是某主题/事件下的一批舆情条目（含标题、摘要、来源、时间、情感标签）。

【条目列表】：
{items_text}

请基于这些真实条目，输出该主题/事件的「六维舆情分析」，仅输出一个 JSON 对象，不要输出任何解释、不要用 Markdown 代码块包裹：

1. basic 基础信息：volume（声量概述）、voice_profile（发声画像）、expression（表达方式），各一句话
2. emotion 情感与态度：sentiment（情感性质概述）、attitude_strength（态度强度）、emotion_shift（情感迁移），各一句话；stance_split（立场分化，百分比对象，和为100）
3. narrative 叙事与框架：issue_frame（议题框架）、symbol_metaphor（符号隐喻）、attribution（归因）、appeal（诉求），各一句话
4. spread 传播结构：path（传播路径）、kols（意见领袖）、platform（平台情况）、reversal（是否反转），各一句话
5. behavior 行为倾向：offline_action（线下行动）、consumption（消费影响）、institutional（制度化参与）、info_seeking（信息搜寻），各一句话
6. deep_impact 深层影响：social_emotion（社会情绪）、group_diff（群体差异）、value_conflict（价值观冲突）、historical_analogy（历史类比），各一句话
7. actions 建议优先行动：3~4 条，每条 {{priority: 1-3 的整数（1 为最高优先级）, title: 行动标题（12字内）, detail: 行动说明（30字内）}}

约束：只能基于给定条目归纳，禁止编造条目中不存在的信息；证据不足时该字段写「暂无足够数据」。

输出格式（严格按此结构）：
{{"basic":{{"volume":"","voice_profile":"","expression":""}},"emotion":{{"sentiment":"","attitude_strength":"","stance_split":{{"支持":0,"反对":0,"中立":0,"理中客":0}},"emotion_shift":""}},"narrative":{{"issue_frame":"","symbol_metaphor":"","attribution":"","appeal":""}},"spread":{{"path":"","kols":"","platform":"","reversal":""}},"behavior":{{"offline_action":"","consumption":"","institutional":"","info_seeking":""}},"deep_impact":{{"social_emotion":"","group_diff":"","value_conflict":"","historical_analogy":""}},"actions":[{{"priority":1,"title":"","detail":""}}]}}"""

# 舆情研判分析报告 Prompt（对齐 PRD(4)《舆情研判分析报告模板》8 段式，输出 Markdown）
PROMPT_REPORT = """你是一位专业的舆情分析师。请基于以下舆情数据，生成一份《舆情研判分析报告》。

【舆情统计】
{metrics_text}

【舆情条目列表】
{items_text}

请严格按照以下 8 段结构输出报告（Markdown 格式，用 # 二级标题）：

# 舆情研判分析报告

## 一、报告概述
### 1.1 报告背景（因何启动本次分析、服务对象、要回答的核心问题）
### 1.2 分析时间范围（覆盖时间段，若有对比期一并说明）
### 1.3 数据来源（渠道范围与监测条件，说明是否去重及主要局限）

## 二、舆情概况
### 2.1 舆情总量（总量及正/中/负条数与占比，注明有效样本量）
### 2.2 舆情趋势（升/降与拐点，关键峰值对应的事件）
### 2.3 主要事件（按时间列 3~5 个关键事件：发生了什么、传播概况、为何重要）

## 三、舆情传播分析
### 3.1 传播渠道分析（各渠道声量占比或排名，指出主阵地与变化）
### 3.2 传播路径分析（从发源到扩散的大致路径及升温快慢）
### 3.3 传播节点分析（关键账号/媒体及其作用）

## 四、舆情内容分析
### 4.1 热点话题分析（每个热点：谈什么、热度、舆论主要看法）
### 4.2 关键词分析（最能代表舆论焦点的若干词，各一句话解释）
### 4.3 情感分析（正/中/负占比；若负面集中，说明主要槽点方向）

## 五、舆情主体分析
### 5.1 主要舆情主体（谁在被讨论、谁在发声，抓头部）
### 5.2 主体行为分析（各方表态、回应或沉默，对舆论走向的影响）

## 六、舆情影响分析
### 6.1 影响范围（波及的平台、人群或地域；不清则写「暂无法细分」）
### 6.2 影响程度（高/中/低评级并各附一句依据，避免夸大）

## 七、舆情应对建议
### 7.1 应对策略（按优先级列条目，须可执行、可复核）
### 7.2 风险预警（可能触发点、关注信号、防范或预案动作）

## 八、总结与展望
### 8.1 总结（当前态势一句话 + 最重要的两三点发现）
### 8.2 展望（谨慎预判 + 下一步持续跟踪重点）

约束：
- 只能基于给定数据归纳，禁止编造；无数据处写「暂无足够数据」。
- 影响程度须给高/中/低评级。
- 报告末尾附一句：本报告基于网络公开舆情生成，策略建议须人工复核后对外使用。"""

_EMOTIONS = {"愤怒", "恐惧", "悲伤", "嘲讽", "幸灾乐祸", "感恩", "自豪", "无"}
_POLARITY = {"正面", "中性", "负面"}
_STANCE = {"支持", "反对", "中立", "理中客"}


# ============================================================
# 一、LLM 调用（OpenAI 兼容 /chat/completions）
# ============================================================
def _chat(messages, temperature=0.0, max_tokens=800):
    base_url, api_key, model = _llm_env()
    if not api_key:
        return None
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[LLM] 调用失败：{e}")
        return None
    content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not content and resp.get("error"):
        print(f"[LLM] 接口返回错误：{resp.get('error')}")
    return content


def _parse_json(text: str):
    """从 LLM 返回中稳健提取 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 去除 markdown 代码块包裹
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


# ============================================================
# 二、单条情感标注（细分情绪 / 态度强度 / 立场 / 理由）
# ============================================================
def _validate_annotation(obj):
    if not isinstance(obj, dict):
        return "输出必须是 JSON 对象"
    if obj.get("sentiment_polarity") not in _POLARITY:
        return "sentiment_polarity 取值非法"
    if obj.get("emotion") not in _EMOTIONS:
        return "emotion 取值非法"
    try:
        strength = int(obj.get("attitude_strength"))
        if not 1 <= strength <= 5:
            return "attitude_strength 须为 1-5 整数"
    except (TypeError, ValueError):
        return "attitude_strength 非法"
    if obj.get("stance") not in _STANCE:
        return "stance 取值非法"
    try:
        conf = float(obj.get("confidence"))
        if not 0 <= conf <= 1:
            return "confidence 须在 0-1"
    except (TypeError, ValueError):
        return "confidence 非法"
    if not isinstance(obj.get("reason"), str):
        return "reason 缺失"
    return None


def annotate_emotion(text: str, retries: int = 3):
    """单条文本情感标注（LLM 归纳）。返回结构化标注 + review_flag。

    注意：情感性质(正/中/负)以 sentiment_analyze 的 API 打标为准，本函数可作交叉校验，
    二者不一致时以 API 为准并在 review_flag 提示人工复核。
    """
    if not _llm_env()[1]:
        return {"annotation": None, "review_flag": True,
                "error": "未配置 LLM_API_KEY，跳过 LLM 归纳（对齐 PRD：缺数据不编造）"}

    last_err = None
    for _ in range(max(1, retries)):
        content = _chat([{"role": "user", "content": PROMPT_ANNOTATE.format(text=text[:5000])}])
        obj = _parse_json(content)
        if obj is None:
            last_err = "LLM 返回无法解析为 JSON"
            continue
        err = _validate_annotation(obj)
        if err:
            last_err = err
            continue
        return {
            "annotation": {
                "sentiment_polarity": obj["sentiment_polarity"],
                "emotion": obj["emotion"],
                "attitude_strength": int(obj["attitude_strength"]),
                "stance": obj["stance"],
                "confidence": float(obj["confidence"]),
                "reason": (obj.get("reason") or "").strip()[:50],
            },
            "review_flag": True,  # AI 归纳块须附人工复核提示（对齐 PRD）
            "error": None,
        }
    return {"annotation": None, "review_flag": True, "error": last_err or "重试后仍失败"}


# ============================================================
# 三、主题/事件级「情感与态度」归纳（态度强度 / 立场分化 / 情感迁移 / 归因）
# ============================================================
def _validate_summary(obj):
    if not isinstance(obj, dict):
        return "输出必须是 JSON 对象"
    for key in ("attitude_strength", "stance_split"):
        if not isinstance(obj.get(key), dict):
            return f"{key} 缺失"
    if not isinstance(obj.get("emotion_shift"), str):
        return "emotion_shift 缺失"
    if obj.get("dominant_emotion") not in _EMOTIONS:
        return "dominant_emotion 取值非法"
    if not isinstance(obj.get("reasons"), list):
        return "reasons 缺失"
    return None


def _items_to_text(items, limit=80):
    lines = []
    for it in items[:limit]:
        senti = (it.get("sentiment") or {}).get("label", "未标注") if isinstance(it.get("sentiment"), dict) else "未标注"
        t = it.get("publish_time") or ""
        text = ((it.get("title") or "") + " " + (it.get("content") or ""))[:120]
        lines.append(f"- [{t}] [{senti}] {text}")
    return "\n".join(lines)


def summarize_emotion_attitude(items, retries: int = 3):
    """主题/事件级「情感与态度」归纳，输出六维 emotion 子维度 + review_flag。"""
    if not _llm_env()[1] or not items:
        return {
            "emotion": {
                "sentiment": None,  # 情感性质由 sentiment_analyze 统计给出，本函数不越界
                "attitude_strength": None,
                "stance_split": None,
                "emotion_shift": None,
                "dominant_emotion": None,
                "reasons": [],
                "confidence": 0.0,
            },
            "review_flag": True,
            "error": "未配置 LLM_API_KEY 或无条目，跳过归纳",
        }

    last_err = None
    for _ in range(max(1, retries)):
        content = _chat([{"role": "user",
                          "content": PROMPT_SUMMARIZE.format(items_text=_items_to_text(items))}])
        obj = _parse_json(content)
        if obj is None:
            last_err = "LLM 返回无法解析为 JSON"
            continue
        err = _validate_summary(obj)
        if err:
            last_err = err
            continue
        return {
            "emotion": {
                "sentiment": None,
                "attitude_strength": obj["attitude_strength"],
                "stance_split": obj["stance_split"],
                "emotion_shift": obj["emotion_shift"],
                "dominant_emotion": obj["dominant_emotion"],
                "reasons": [r[:30] for r in obj.get("reasons", [])][:3],
                "confidence": float(obj.get("confidence", 0.0)),
            },
            "review_flag": True,
            "error": None,
        }
    return {"emotion": None, "review_flag": True, "error": last_err or "重试后仍失败"}


# ============================================================
# 四、六维舆情分析（LLM 归纳，对齐 PRD(4) 六维）
# ============================================================
_SIXDIM_FIELDS = {
    "basic": ["volume", "voice_profile", "expression"],
    "emotion": ["sentiment", "attitude_strength", "emotion_shift"],
    "narrative": ["issue_frame", "symbol_metaphor", "attribution", "appeal"],
    "spread": ["path", "kols", "platform", "reversal"],
    "behavior": ["offline_action", "consumption", "institutional", "info_seeking"],
    "deep_impact": ["social_emotion", "group_diff", "value_conflict", "historical_analogy"],
}


def _validate_sixdim(obj):
    if not isinstance(obj, dict):
        return "输出必须是 JSON 对象"
    for key in _SIXDIM_FIELDS:
        if not isinstance(obj.get(key), dict):
            return f"{key} 维度缺失"
    return None


def _fill_defaults(obj):
    """补齐六维缺失字段，保证前端渲染不报错。"""
    out = {}
    for dim, fields in _SIXDIM_FIELDS.items():
        src = obj.get(dim) or {}
        out[dim] = {f: str(src.get(f) or "暂无足够数据") for f in fields}
    ss = (obj.get("emotion") or {}).get("stance_split")
    out["emotion"]["stance_split"] = ss if isinstance(ss, dict) else {"支持": 0, "反对": 0, "中立": 0, "理中客": 0}
    # actions：建议优先行动（LLM 归纳）
    acts = obj.get("actions")
    if isinstance(acts, list):
        out["actions"] = [{
            "priority": int(a.get("priority", 3)),
            "title": str(a.get("title") or "待定行动"),
            "detail": str(a.get("detail") or ""),
        } for a in acts[:4] if isinstance(a, dict)]
    else:
        out["actions"] = []
    return out


def summarize_sixdimensions(items, retries: int = 2):
    """六维舆情分析（LLM 归纳）。返回 {six_dimensions, review_flag, error}。"""
    if not _llm_env()[1] or not items:
        return {"six_dimensions": None, "review_flag": True,
                "error": "未配置 LLM_API_KEY 或无条目，跳过六维归纳"}

    last_err = None
    for _ in range(max(1, retries)):
        content = _chat([{"role": "user",
                          "content": PROMPT_SIXDIM.format(items_text=_items_to_text(items))}],
                        max_tokens=2500)
        obj = _parse_json(content)
        if obj is None:
            last_err = "LLM 返回无法解析为 JSON"
            continue
        err = _validate_sixdim(obj)
        if err:
            last_err = err
            continue
        return {
            "six_dimensions": _fill_defaults(obj),
            "review_flag": True,
            "error": None,
        }
    return {"six_dimensions": None, "review_flag": True, "error": last_err or "重试后仍失败"}


# ============================================================
# 五、舆情研判分析报告（对齐 PRD(4) 8 段式模板，输出 Markdown）
# ============================================================
def generate_report(metrics_text: str, items, retries: int = 2):
    """生成《舆情研判分析报告》（Markdown）。返回 {report, review_flag, error}。"""
    if not _llm_env()[1]:
        return {"report": None, "review_flag": True, "error": "未配置 LLM_API_KEY，跳过报告生成"}
    if not items:
        return {"report": None, "review_flag": True, "error": "无舆情数据，无法生成报告"}

    last_err = None
    for _ in range(max(1, retries)):
        content = _chat([{"role": "user",
                          "content": PROMPT_REPORT.format(
                              metrics_text=metrics_text, items_text=_items_to_text(items))}],
                        max_tokens=2800)
        if not content:
            last_err = "LLM 返回为空"
            continue
        return {"report": content, "review_flag": True, "error": None}
    return {"report": None, "review_flag": True, "error": last_err or "重试后仍失败"}


if __name__ == "__main__":
    if not _llm_env()[1]:
        print("未配置 LLM_API_KEY，展示 Prompt 与降级行为（不联网）：")
        print("--- 单条标注 Prompt ---")
        print(PROMPT_ANNOTATE.format(text="示例文本"))
        print("\n--- 空归纳结果 ---")
        print(annotate_emotion("示例文本"))
        print(summarize_emotion_attitude([]))
    else:
        demo = annotate_emotion("某品牌因产品质量问题被大量用户投诉，并宣布召回，网友表示非常愤怒。")
        print(demo)
