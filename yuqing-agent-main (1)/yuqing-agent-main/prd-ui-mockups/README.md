# 舆情分析系统 · 可交互原型

打开 `index.html`。已从「纯 Agent」改为**完整系统**，并吸收参考站架构，但做了功能合并。

## 信息架构（相对原系统的简化）

| 顶栏模块 | 作用 | 说明 |
|----------|------|------|
| 工作台 | 总览 KPI / 走势 / 需关注 | 类似参考站首页 |
| 主题监测 | 主题列表 + 筛选 + 数据列表 + 方案管理 | **合并**原「监测分析 / 数据监测 / 监测管理」 |
| 全文搜索 | 独立检索 | 不依赖助手 |
| 事件分析 | 任务列表 + 对话/表单创建 | 保留 |
| 智能助手 | 辅助配置与研判 | 系统能力之一，不再是唯一壳 |

> 已去掉「监测大屏」模块与相关入口。

## 可互动操作

- 顶栏切换模块
- 主题监测：切换主题、筛选、数据列表 / 方案管理
- **新建监测**：左侧高级创建表单 + 右侧助手填表；可手改字段，也可对话改
- 全文搜索：输入/历史/热词搜索
- **事件分析创建**：左侧「创建事件分析任务」表单 + 右侧助手填表；可手改或对话修改
- 智能助手：生成参数卡片并写回监测

## 设计原则（可写进 PRD）

1. 系统为主，助手为辅。  
2. 用户可独立完成搜索与列表筛查。  
3. 顶栏避免把同一监测能力拆成多个入口。

## 情感分析引擎（对齐 PRD(4) 第五章）

默认情况下，文章情感标签沿用 `data/articles.json` 里的预置值。接入引擎后可**真实计算**并**切换 API**：

- `sentiment.py`：入库批量打标，provider 可插拔 `rule`（离线词典，默认）/ `aliyun` / `xfyun` / `selfhosted`（私有化 RoBERTa/BERT）。
- `insight_llm.py`：态度强度 / 立场 / 情感迁移 / 理由的 LLM 归纳（OpenAI 兼容协议）。

**用法（PowerShell，在 `prd-ui-mockups` 目录下）：**

```powershell
# 启动时用情感引擎重算全部文章（默认 rule 词典）
$env:SENTIMENT_RECOMPUTE = "1"
py server.py 8091

# 或运行中触发重算：浏览器打开 http://127.0.0.1:8091/api/recompute-sentiment

# 切换阿里云 / 讯飞 / 私有化（同 sentiment.py 的环境变量）
$env:SENTIMENT_PROVIDER = "aliyun"
$env:ALIYUN_AK_ID       = "..."
$env:ALIYUN_AK_SECRET   = "..."
$env:SENTIMENT_RECOMPUTE = "1"
py server.py 8091

# 让「智能助手」在问答时追加 LLM 情感归纳（需配置大模型）
$env:LLM_BASE_URL = "https://api.openai.com/v1"
$env:LLM_API_KEY  = "sk-..."
$env:LLM_MODEL    = "gpt-4o-mini"
py server.py 8091
```

未配置 key 时，引擎自动降级到 `rule` 词典；`LLM_API_KEY` 未配置时助手不联网、不编造，返回现有规则回答。
