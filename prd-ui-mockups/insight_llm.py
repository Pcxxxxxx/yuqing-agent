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
PROMPT_SIXDIM = """你是一位专业的舆情分析师。以下是某主题/事件下的一批舆情条目（含 id、标题、摘要、来源、时间、情感标签）。

【舆情统计】（真实统计。声量、情感占比、来源构成必须以这里为准，禁止另写一套数字）
{metrics_text}

【条目列表】：
{items_text}

请基于这些真实条目，输出该主题/事件的「六维舆情分析」，仅输出一个 JSON 对象，不要输出任何解释、不要用 Markdown 代码块包裹：

1. basic 基础信息：volume（声量概述）、voice_profile（发声画像）、expression（表达方式），各一句话
2. emotion 情感与态度：sentiment（情感性质概述）、attitude_strength（态度强度）、emotion_shift（情感迁移），各一句话；stance_split（立场分化，百分比对象，和为100；此为估计，须与统计情感分布区分）
3. narrative 叙事与框架：issue_frame（议题框架）、symbol_metaphor（符号隐喻）、attribution（归因）、appeal（诉求），各一句话
4. spread 传播结构：path（传播路径）、kols（意见领袖）、platform（平台情况）、reversal（是否反转），各一句话。无转发链数据时写「暂无足够数据」或仅概括来源构成
5. behavior 行为倾向：offline_action（线下行动）、consumption（消费影响）、institutional（制度化参与）、info_seeking（信息搜寻），各一句话
6. deep_impact 深层影响：social_emotion（社会情绪）、group_diff（群体差异）、value_conflict（价值观冲突）、historical_analogy（历史类比），各一句话
7. actions 建议优先行动：3~4 条，每条 {{priority: 1-3 的整数（1 为最高优先级）, title: 行动标题（12字内）, detail: 行动说明（30字内）}}
8. evidence 原文依据：为每个不是「暂无足够数据」的结论字段列 2～4 条。每条 {{field: 字段路径, id: 条目id整数, quote: 从该条目原文逐字摘录20～60字}}。field 必须是下列之一：
   basic.volume, basic.voice_profile, basic.expression,
   emotion.sentiment, emotion.attitude_strength, emotion.emotion_shift,
   narrative.issue_frame, narrative.symbol_metaphor, narrative.attribution, narrative.appeal,
   spread.path, spread.kols, spread.platform, spread.reversal,
   behavior.offline_action, behavior.consumption, behavior.institutional, behavior.info_seeking,
   deep_impact.social_emotion, deep_impact.group_diff, deep_impact.value_conflict, deep_impact.historical_analogy,
   actions

约束：只能基于给定条目归纳，禁止编造；quote 必须是对应 id 条目标题或正文的连续原文，禁止改写；证据不足时该字段写「暂无足够数据」且不要硬凑 evidence。

输出格式（严格按此结构）：
{{"basic":{{"volume":"","voice_profile":"","expression":""}},"emotion":{{"sentiment":"","attitude_strength":"","stance_split":{{"支持":0,"反对":0,"中立":0,"理中客":0}},"emotion_shift":""}},"narrative":{{"issue_frame":"","symbol_metaphor":"","attribution":"","appeal":""}},"spread":{{"path":"","kols":"","platform":"","reversal":""}},"behavior":{{"offline_action":"","consumption":"","institutional":"","info_seeking":""}},"deep_impact":{{"social_emotion":"","group_diff":"","value_conflict":"","historical_analogy":""}},"actions":[{{"priority":1,"title":"","detail":""}}],"evidence":[{{"field":"emotion.emotion_shift","id":0,"quote":""}}]}}"""

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
        src = (it.get("source") or "").strip()
        src_bit = f" [{src}]" if src else ""
        aid = it.get("id")
        id_bit = f"[id={aid}] " if aid is not None and aid != "" else ""
        text = ((it.get("title") or "") + " " + (it.get("content") or ""))[:160]
        lines.append(f"- {id_bit}[{t}]{src_bit} [{senti}] {text}")
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


def summarize_sixdimensions(items, retries: int = 2, metrics_text: str = ""):
    """六维舆情分析（LLM 归纳）。返回 {six_dimensions, evidence, review_flag, error}。"""
    if not _llm_env()[1] or not items:
        return {"six_dimensions": None, "evidence": [], "review_flag": True,
                "error": "未配置 LLM_API_KEY 或无条目，跳过六维归纳"}

    last_err = None
    def _brace_escape(s):
        return (s or "").replace("{", "{{").replace("}", "}}")
    prompt = PROMPT_SIXDIM.format(
        items_text=_brace_escape(_items_to_text(items)),
        metrics_text=_brace_escape(metrics_text or "暂无统计"),
    )
    for _ in range(max(1, retries)):
        content = _chat([{"role": "user", "content": prompt}], max_tokens=4000)
        obj = _parse_json(content)
        if obj is None:
            last_err = "LLM 返回无法解析为 JSON"
            continue
        err = _validate_sixdim(obj)
        if err:
            last_err = err
            continue
        evidence = obj.get("evidence") if isinstance(obj.get("evidence"), list) else []
        return {
            "six_dimensions": _fill_defaults(obj),
            "evidence": evidence,
            "review_flag": True,
            "error": None,
        }
    return {"six_dimensions": None, "evidence": [], "review_flag": True,
            "error": last_err or "重试后仍失败"}


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


# ============================================================
# 六、应对方案 / 分型报告（日常 / 格局 / 危机；规则分流 + 人工确认模板后生成）
# ============================================================
PLAN_TEMPLATE_IDS = ("daily", "compete", "crisis")
PLAN_TITLES = {
    "daily": "日常/定期舆情应对方案",
    "compete": "市场竞争格局应对方案",
    "crisis": "突发事件舆情应对方案",
}
_NEG_RATIO_CRISIS = 50.0
_SPIKE_RATIO = 1.8
_CRISIS_HINTS = ("反转", "投诉", "监管", "召回", "聚集", "游行", "维权", "诉讼", "抵制", "爆炸", "伤亡", "危机")
_COMPETE_NAME_HINTS = ("生态", "新能源", "终端", "竞品", "格局", "对比", "对标", "市场份额", "苹果", "华为")

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

PROMPT_PLAN = """你是一位专业的舆情分析师。请基于给定数据，按指定大纲生成一份 Markdown 文档。

【监测主题/事件】：{topic_name}
【选用模板】：{template_title}（id={template_id}）
【舆情统计】
{metrics_text}

【六维研判摘要】
{sixdim_text}

【建议优先行动】
{actions_text}

【舆情条目列表】
{items_text}

请严格按照以下大纲的标题层级输出（不要增删章节，标题文字与大纲保持一致）：

{outline}

{extra_rule}

约束：
- 只能基于给定统计、六维摘要、优先行动和条目归纳，禁止编造条目中不存在的账号、声明、产品或竞品。
- 无数据处写「暂无足够数据」，不要用行业常识补全。
- 三类文档第 1 章均为速览，篇幅不超过全文约 30%；第 2 章起必须是可执行的应对、预案与准则。
- 日常类：已有独立研判报告，禁止再写声量走势、渠道表现、大事记、热点长文；不要写成战时危机方案。
- 格局类：以「{topic_name}」为监测对象；竞品名称仅可摘自条目；禁止把 SOV、口碑、营销战役再写成独立大章；无双边数据写「暂无足够数据」，禁止编造对标表。
- 危机类：禁止再单列传播分析、观点摘录、风险研判大章；第 2–7 章必须是定级、处置清单、分情景预案、沟通准则、监测复盘与人工复核。
- 优先行动必须对应六维槽点与「建议优先行动」，不要另起一套优先级。
- 文档末尾附一句：本方案基于网络公开舆情生成，策略建议须人工复核后对外使用。"""

_EXTRA_RULES = {
    "daily": "这是例行应对方案，不是监测分析报告（分析已由研判报告承担）。第 1 章只写速览，不超过全文约 30%。其余全部写本周必做、分情景处置和准则。不要展开趋势图解读、渠道排行、每日大事记。不要写成危机战时方案。",
    "compete": "这是竞争应对方案。第 1 章可保留对比结论，但不超过全文约 30%。其余全部写反制清单、分情景预案和沟通准则。不要展开 SOV 长表、功能卖点逐条分析、营销战役复盘。竞品名只能来自条目。",
    "crisis": "这是应对方案，不是舆情分析报告。第 1 章只写速览，篇幅不超过全文约 30%。其余章节全部写可执行的处置、预案和准则；不要展开声量趋势、渠道分析、网民观点摘录。禁止编造已发布的声明或未出现的责任人姓名，职务用角色（如公关负责人）即可。",
}


def load_plan_outline(template_id: str) -> str:
    path = os.path.join(_TEMPLATES_DIR, f"{template_id}.md")
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def sixdim_brief(sixdim) -> str:
    if not isinstance(sixdim, dict):
        return "暂无六维分析"
    e = sixdim.get("emotion") or {}
    n = sixdim.get("narrative") or {}
    s = sixdim.get("spread") or {}
    b = sixdim.get("behavior") or {}
    return "\n".join([
        f"情感：{e.get('sentiment') or '暂无足够数据'}；态度强度：{e.get('attitude_strength') or '暂无足够数据'}；迁移：{e.get('emotion_shift') or '暂无足够数据'}",
        f"议题框架：{n.get('issue_frame') or '暂无足够数据'}；诉求：{n.get('appeal') or '暂无足够数据'}",
        f"主阵地：{s.get('platform') or '暂无足够数据'}；是否反转：{s.get('reversal') or '暂无足够数据'}；传播路径：{s.get('path') or '暂无足够数据'}",
        f"线下行动：{b.get('offline_action') or '暂无足够数据'}；制度化参与：{b.get('institutional') or '暂无足够数据'}",
    ])


def actions_brief(sixdim) -> str:
    acts = (sixdim or {}).get("actions") if isinstance(sixdim, dict) else None
    if not acts:
        return "暂无优先行动"
    lines = []
    for a in acts:
        if not isinstance(a, dict):
            continue
        lines.append(f"- [P{a.get('priority', 3)}] {a.get('title') or ''}：{a.get('detail') or ''}")
    return "\n".join(lines) or "暂无优先行动"


def _text_has_crisis_hint(*parts):
    blob = " ".join(str(p or "") for p in parts)
    return any(k in blob for k in _CRISIS_HINTS)


def _is_volume_spike(trend):
    if not isinstance(trend, list) or len(trend) < 4:
        return False
    counts = []
    for t in trend:
        try:
            counts.append(int((t or {}).get("count") or 0))
        except (TypeError, ValueError):
            counts.append(0)
    early = counts[:-2]
    late = counts[-2:]
    early_avg = sum(early) / max(1, len(early))
    late_avg = sum(late) / max(1, len(late))
    if early_avg <= 0:
        return late_avg >= 8
    return late_avg >= _SPIKE_RATIO * early_avg


def _looks_compete(topic_name: str) -> bool:
    name = topic_name or ""
    return any(k in name for k in _COMPETE_NAME_HINTS)


def route_plan_template(metrics, sixdim, scope="", topic_name=""):
    """规则推荐模板。永不默认 crisis。返回 {template_id, title, reasons, confidence}。"""
    reasons = []
    emo = (metrics or {}).get("emotion") or {}
    try:
        neg_ratio = float(emo.get("neg_ratio") or 0)
    except (TypeError, ValueError):
        neg_ratio = 0.0
    trend = (metrics or {}).get("trend") or []
    spread = (sixdim or {}).get("spread") or {}
    behavior = (sixdim or {}).get("behavior") or {}
    narrative = (sixdim or {}).get("narrative") or {}

    is_event = str(scope or "").startswith("event:")
    spike = _is_volume_spike(trend)
    crisis_text = _text_has_crisis_hint(
        spread.get("reversal"), behavior.get("offline_action"), behavior.get("institutional"),
        spread.get("path"), narrative.get("appeal"),
    )

    if is_event:
        reasons.append("当前范围是事件分析任务")
    if neg_ratio >= _NEG_RATIO_CRISIS:
        reasons.append(f"负面占比 {neg_ratio:.1f}%（≥{_NEG_RATIO_CRISIS:.0f}%）")
    if spike:
        reasons.append("近 7 日声量末段明显高于前期")
    if crisis_text:
        reasons.append("六维中出现反转/投诉/监管/召回等危机信号")

    if is_event or neg_ratio >= _NEG_RATIO_CRISIS or spike or crisis_text:
        tid = "crisis"
        conf = 0.86 if (is_event or crisis_text) else 0.72
        return {
            "template_id": tid,
            "title": PLAN_TITLES[tid],
            "reasons": reasons[:3] or ["命中危机分流规则"],
            "confidence": conf,
        }

    if _looks_compete(topic_name):
        return {
            "template_id": "compete",
            "title": PLAN_TITLES["compete"],
            "reasons": [f"主题「{topic_name}」更像竞品/行业对标，而非突发处置"],
            "confidence": 0.7,
        }

    return {
        "template_id": "daily",
        "title": PLAN_TITLES["daily"],
        "reasons": ["未命中突发/危机信号，按例行应对方案推荐", "详细数据分析请用「生成研判报告」；若要写战时处置请改选危机模板"],
        "confidence": 0.65,
    }


def generate_plan(template_id, metrics_text, sixdim, items, topic_name="", retries: int = 2):
    """按已确认的 template_id 生成分型报告/应对方案（Markdown）。"""
    if template_id not in PLAN_TEMPLATE_IDS:
        return {"plan": None, "title": None, "template_id": template_id, "review_flag": True,
                "error": "未知模板，请选择日常/格局/危机之一"}
    if not _llm_env()[1]:
        return {"plan": None, "title": PLAN_TITLES[template_id], "template_id": template_id,
                "review_flag": True, "error": "未配置 LLM_API_KEY，跳过方案生成"}
    if not items:
        return {"plan": None, "title": PLAN_TITLES[template_id], "template_id": template_id,
                "review_flag": True, "error": "无舆情数据，无法生成方案"}
    if not sixdim:
        return {"plan": None, "title": PLAN_TITLES[template_id], "template_id": template_id,
                "review_flag": True, "error": "请先生成六维分析"}

    try:
        outline = load_plan_outline(template_id)
    except OSError:
        return {"plan": None, "title": PLAN_TITLES[template_id], "template_id": template_id,
                "review_flag": True, "error": "模板文件缺失"}

    prompt = PROMPT_PLAN.format(
        topic_name=topic_name or "未命名主题",
        template_title=PLAN_TITLES[template_id],
        template_id=template_id,
        metrics_text=metrics_text or "",
        sixdim_text=sixdim_brief(sixdim),
        actions_text=actions_brief(sixdim),
        items_text=_items_to_text(items),
        outline=outline,
        extra_rule=_EXTRA_RULES.get(template_id, ""),
    )
    last_err = None
    for _ in range(max(1, retries)):
        content = _chat([{"role": "user", "content": prompt}], max_tokens=2800)
        if not content:
            last_err = "LLM 返回为空"
            continue
        return {
            "plan": content,
            "title": PLAN_TITLES[template_id],
            "template_id": template_id,
            "review_flag": True,
            "error": None,
        }
    return {"plan": None, "title": PLAN_TITLES[template_id], "template_id": template_id,
            "review_flag": True, "error": last_err or "重试后仍失败"}


# ============================================================
# 七、推理依据：核验摘录、对齐统计、组装前端抽屉数据
# ============================================================
_BASIS_FIELDS = [
    ("basic.volume", "1. 基础信息", "声量"),
    ("basic.voice_profile", "1. 基础信息", "发声画像"),
    ("basic.expression", "1. 基础信息", "表达方式"),
    ("emotion.sentiment", "2. 情感与态度", "情感性质"),
    ("emotion.attitude_strength", "2. 情感与态度", "态度强度"),
    ("emotion.stance_split", "2. 情感与态度", "立场分化"),
    ("emotion.emotion_shift", "2. 情感与态度", "情感迁移"),
    ("narrative.issue_frame", "3. 叙事与框架", "议题框架"),
    ("narrative.symbol_metaphor", "3. 叙事与框架", "符号隐喻"),
    ("narrative.attribution", "3. 叙事与框架", "归因"),
    ("narrative.appeal", "3. 叙事与框架", "诉求"),
    ("spread.path", "4. 传播结构", "传播路径"),
    ("spread.kols", "4. 传播结构", "意见领袖"),
    ("spread.platform", "4. 传播结构", "平台情况"),
    ("spread.reversal", "4. 传播结构", "是否反转"),
    ("behavior.offline_action", "5. 行为倾向", "线下行动"),
    ("behavior.consumption", "5. 行为倾向", "消费影响"),
    ("behavior.institutional", "5. 行为倾向", "制度化参与"),
    ("behavior.info_seeking", "5. 行为倾向", "信息搜寻"),
    ("deep_impact.social_emotion", "6. 深层影响", "社会情绪"),
    ("deep_impact.group_diff", "6. 深层影响", "群体差异"),
    ("deep_impact.value_conflict", "6. 深层影响", "价值观冲突"),
    ("deep_impact.historical_analogy", "6. 深层影响", "历史类比"),
]
_STATS_REF = {
    "basic.volume": ["total", "trend"],
    "basic.voice_profile": ["source"],
    "emotion.sentiment": ["emotion"],
    "spread.platform": ["source"],
}
_DEFAULT_EXPAND = {
    "emotion.emotion_shift",
    "narrative.issue_frame", "narrative.symbol_metaphor", "narrative.attribution", "narrative.appeal",
    "deep_impact.social_emotion", "deep_impact.group_diff",
    "deep_impact.value_conflict", "deep_impact.historical_analogy",
}
_EMPTY_CLAIMS = {"", "暂无足够数据", "None", "null"}
_PUNCT_RE = re.compile(r"[\s\u3000，。！？、；：\"'“”‘’《》【】（）()\[\].,!?;:]+")


def _strip_punct(s):
    return _PUNCT_RE.sub("", s or "")


def _verify_quote(quote, item):
    """摘录是否出现在条目标题或正文中（去空白后精确匹配，再尝试去标点）。"""
    q = (quote or "").strip()
    if len(q) < 8:
        return False
    blob = (item.get("title") or "") + (item.get("content") or "")
    compact_q = re.sub(r"\s+", "", q)
    compact_blob = re.sub(r"\s+", "", blob)
    if compact_q and compact_q in compact_blob:
        return True
    q2, b2 = _strip_punct(q), _strip_punct(blob)
    return bool(q2) and len(q2) >= 8 and q2 in b2


def _claim_of(sixdim, field_key):
    dim, name = field_key.split(".", 1)
    val = (sixdim.get(dim) or {}).get(name)
    if isinstance(val, dict):
        return "、".join(f"{k}{v}%" for k, v in val.items())
    return str(val or "")


def _feed_index(feed):
    idx = {}
    for it in feed or []:
        try:
            idx[int(it.get("id"))] = it
        except (TypeError, ValueError):
            continue
    return idx


def _first_count(text):
    m = re.search(r"(\d{1,5})\s*条", text or "")
    return int(m.group(1)) if m else None


def _stats_conflict(field_key, claim, metrics):
    """结论里的数字/表述是否与真实统计明显不一致。"""
    if not metrics or not claim or claim in _EMPTY_CLAIMS:
        return False
    emo = metrics.get("emotion") or {}
    try:
        neg = float(emo.get("neg_ratio") or 0)
        pos = float(emo.get("pos_ratio") or 0)
    except (TypeError, ValueError):
        neg, pos = 0.0, 0.0
    if field_key == "basic.volume":
        n = _first_count(claim)
        total = metrics.get("total")
        if n is not None and total is not None and abs(n - int(total)) > max(3, int(int(total) * 0.15)):
            return True
    if field_key == "emotion.sentiment":
        if re.search(r"负面.{0,8}(过半|为主|占优)", claim) or re.search(r"(过半|为主).{0,8}负面", claim):
            if neg < 50:
                return True
        if re.search(r"正面.{0,8}(过半|为主|占优)", claim) or re.search(r"(过半|为主).{0,8}正面", claim):
            if pos < 50:
                return True
        m = re.search(r"负面\s*(\d{1,3}(?:\.\d+)?)\s*%", claim)
        if m and abs(float(m.group(1)) - neg) > 8:
            return True
        m = re.search(r"正面\s*(\d{1,3}(?:\.\d+)?)\s*%", claim)
        if m and abs(float(m.group(1)) - pos) > 8:
            return True
    return False


def _shape_evidence_item(aid, quote, item, verified, missing=False):
    senti = ""
    if item:
        raw = item.get("sentiment")
        senti = raw.get("label") if isinstance(raw, dict) else (raw or "")
    return {
        "id": aid,
        "title": (item or {}).get("title") or "",
        "source": (item or {}).get("source") or "",
        "time": str((item or {}).get("publish_time") or "")[:16],
        "sentiment": senti,
        "url": (item or {}).get("url") or "",
        "quote": (quote or "").strip()[:80],
        "verified": bool(verified),
        "missing": bool(missing),
    }


def build_basis(sixdim, evidence_raw, feed, metrics, meta):
    """把模型引用的 evidence 核验后，组装成前端「推理依据」抽屉数据。"""
    idx = _feed_index(feed)
    buckets = {k: [] for k, _, _ in _BASIS_FIELDS}
    buckets["actions"] = []
    known = set(buckets)

    for ev in evidence_raw or []:
        if not isinstance(ev, dict):
            continue
        field = str(ev.get("field") or "").strip()
        if field.startswith("actions"):
            field = "actions"
        if field not in known:
            continue
        try:
            aid = int(ev.get("id"))
        except (TypeError, ValueError):
            continue
        quote = str(ev.get("quote") or "").strip()
        item = idx.get(aid)
        if not item:
            buckets[field].append(_shape_evidence_item(aid, quote, None, False, missing=True))
            continue
        buckets[field].append(_shape_evidence_item(aid, quote, item, _verify_quote(quote, item)))

    fields = {}
    no_evidence, unverified, stats_conflict = [], [], []
    for key, dim_title, label in _BASIS_FIELDS:
        claim = _claim_of(sixdim or {}, key)
        empty = claim in _EMPTY_CLAIMS
        evs = (buckets.get(key) or [])[:4]
        conflict = _stats_conflict(key, claim, metrics)
        stats_ref = _STATS_REF.get(key, [])
        stats_match = None
        if stats_ref and not empty:
            stats_match = not conflict
        if empty:
            status = "insufficient"
        elif conflict:
            status = "stats_conflict"
            stats_conflict.append(key)
        elif not evs:
            status = "no_evidence"
            no_evidence.append(key)
        elif any(not e.get("verified") for e in evs):
            status = "unverified"
            unverified.append(key)
        else:
            status = "ok"
        fields[key] = {
            "dim": dim_title,
            "label": label,
            "claim": claim,
            "stats_ref": stats_ref,
            "stats_match": stats_match,
            "expand": key in _DEFAULT_EXPAND or status in ("stats_conflict", "unverified"),
            "status": status,
            "evidence": evs,
        }

    return {
        "meta": {**(meta or {}), "stats": metrics or {}},
        "fields": fields,
        "actions": {
            "items": (sixdim or {}).get("actions") or [],
            "evidence": (buckets.get("actions") or [])[:6],
        },
        "review": {
            "no_evidence": no_evidence,
            "unverified": unverified,
            "stats_conflict": stats_conflict,
        },
    }


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
