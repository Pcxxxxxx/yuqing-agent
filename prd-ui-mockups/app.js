const $ = (sel, root = document) => root.querySelector(sel);

const state = {
  module: "overview", // overview | monitor | search | event | assistant
  monitorView: "list", // list | manage | create | insight
  topicId: "t1",
  filters: {
    time: "7d",
    media: "all",
    sentiment: "all",
  },
  searchQ: "",
  searchDone: false,
  eventMode: "list", // list | create | result
  eventId: "e1",
  assistMode: "topic",
  assistMessages: [],
  modal: null,
  planForm: emptyPlanForm(),
  planChat: [],
  eventForm: emptyEventForm(),
  eventChat: [],
  config: null, // API 配置（脱敏）
  sentimentResult: null, // 情感重算结果
  recomputing: false,
  insightResult: null, // LLM 情感态度归纳结果
  insightLoading: false,
  showAllArticles: false, // 文章明细是否展开全部
  sixdimData: null, // LLM 六维舆情分析结果（当前 scope）
  sixdimLoading: false,
  metricsData: null, // 六维报告真实指标（声量趋势/情感分布/来源构成）
  sixdimScope: null, // 当前六维报告范围（topic:<id> 或 event:<id>）
  sixdimCache: {}, // 各 scope 的六维结果缓存（切换页面不丢）
  reportData: null, // 舆情研判分析报告（markdown）
  reportLoading: false,
  reportCache: {}, // 各 scope 的报告缓存（区分生成/查看/重新生成）
  planCache: {}, // 各 scope 的应对方案 { md, templateId, title }
  planLoading: false,
};

function emptyEventForm() {
  return {
    name: "",
    start: "2026/07/12",
    end: "2026/08/12",
    keywords: "",
    exclude: "",
  };
}

function emptyPlanForm() {
  return {
    name: "",
    group: "中国文化",
    keywords: "",
    exclude: "",
    alert: false,
    alertName: "",
    alertWords: "",
    alertSources: ["全部"],
    alertContent: "全部",
    alertMatch: "全文",
    alertChannel: "系统推送",
    alertWeekend: "关闭",
    alertMerge: "不合并",
    alertDedup: "关闭",
    alertTime: "00:00 - 23:00",
    alertInterval: "实时预警",
    alertIntervalHours: "1小时",
  };
}

// 数据由后端(server.py)从 agent_article 实时提供；启动时 loadData() 拉取
let topics = [];
let articles = [];
let overviewData = null;
let hotWords = [];

const events = [
  { id: "e1", name: "美国关税", createdAt: "2026-08-01", status: "已生成" },
  { id: "e2", name: "某品牌召回传播", createdAt: "2026-07-20", status: "已生成" },
];

// 从后端拉取真实数据（工作台/主题/文章/热词）
async function loadData() {
  try {
    const [ov, tp, ar, hot] = await Promise.all([
      fetch("/api/overview").then((r) => r.json()),
      fetch("/api/topics").then((r) => r.json()),
      fetch("/api/articles?limit=300").then((r) => r.json()),
      fetch("/api/hot").then((r) => r.json()),
    ]);
    overviewData = ov.data;
    topics = (tp.data || []).map((t) => ({
      id: t.id,
      name: t.name,
      meta: `${t.count} 条 · ${t.status}`,
      status: t.status,
      keywords: t.keywords || [],
    }));
    articles = ar.data || [];
    hotWords = hot.data || [];
    if (topics.length && !topics.find((t) => t.id === state.topicId)) {
      state.topicId = topics[0].id;
    }
    if (state.module === "overview") render();
    else render();
  } catch (e) {
    console.error("加载数据失败", e);
  }
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.hidden = true;
  }, 1800);
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function currentTopic() {
  return topics.find((t) => t.id === state.topicId) || topics[0];
}

function filteredArticles() {
  return articles.filter((a) => {
    if (a.topic !== state.topicId) return false;
    if (state.filters.media !== "all" && a.media !== state.filters.media) return false;
    if (state.filters.sentiment !== "all" && a.sentiment !== state.filters.sentiment) return false;
    return true;
  });
}

function setModule(mod) {
  state.module = mod;
  if (mod === "monitor") state.monitorView = "list";
  if (mod === "event") state.eventMode = "list";
  if (mod === "assistant" && !state.assistMessages.length) {
    state.assistMessages = [];
  }
  if (mod === "settings") {
    loadConfig();
    loadSentimentResult();
  }
  render();
}

/** 切换监测主题。resetView=true 时强制回数据列表；否则保留 list/insight/manage */
function switchMonitorTopic(topicId, resetView = false) {
  if (!topicId) return;
  if (state.sixdimLoading) {
    if (topicId === state.topicId && state.module === "monitor") return;
    toast("六维分析生成中，请稍候再切换主题");
    return;
  }
  state.topicId = topicId;
  state.module = "monitor";
  if (resetView || state.monitorView === "create") {
    state.monitorView = "list";
  }
  if (state.monitorView === "insight") {
    state.sixdimScope = "topic:" + state.topicId;
    state.sixdimData = state.sixdimCache[state.sixdimScope] || null;
    loadMetrics();
  }
  render();
}

function openPlanCreate(seedText) {
  state.module = "monitor";
  state.monitorView = "create";
  state.planForm = emptyPlanForm();
  state.planChat = [];
  if (seedText) {
    state.planChat.push({ role: "user", text: seedText });
    applyAgentFill(seedText, false);
    state.planChat.push({
      role: "agent",
      html: `<p>已根据你的描述填写左侧监测方案表单。可直接改字段，或继续告诉我要改哪一项。</p>`,
    });
  }
  render();
}

function applyAgentFill(text, isPatch) {
  const f = state.planForm;
  const t = text.trim();

  // patch intents
  if (/屏蔽|排除|歧义/.test(t)) {
    const add = t.replace(/^.*?(加上|改为|改成|设置[为成]?)/, "").trim() || t;
    if (/清空|去掉全部屏蔽/.test(t)) f.exclude = "";
    else if (/改成|改为/.test(t)) f.exclude = cleanKeywordExpr(add);
    else f.exclude = f.exclude ? `${f.exclude}|${cleanKeywordExpr(add)}` : cleanKeywordExpr(add);
  }
  if (/预警|告警/.test(t)) {
    f.alert = !/关|关闭|不要|关闭预警/.test(t);
    if (f.alert) {
      f.alertName = f.alertName || f.name || "风险预警";
      if (/预警词|告警词/.test(t)) {
        const m = t.match(/(?:预警词|告警词)[为成是:：]?\s*(.+)$/);
        if (m) f.alertWords = m[1].replace(/[，、]/g, ",").trim();
      } else if (!f.alertWords && f.keywords) {
        f.alertWords = f.keywords.replace(/[+|()]/g, ",").replace(/,+/g, ",").replace(/^,|,$/g, "");
      }
      if (/负面|敏感/.test(t)) f.alertContent = "敏感";
      if (/只要微博|仅微博/.test(t)) f.alertSources = ["微博"];
      else if (/只要新闻|仅新闻/.test(t)) f.alertSources = ["新闻"];
      else if (/微博.*新闻|新闻.*微博/.test(t)) f.alertSources = ["微博", "新闻"];
      if (/邮箱/.test(t)) f.alertChannel = "邮箱推送";
      if (/周末/.test(t)) f.alertWeekend = /关|关闭|不要/.test(t) ? "关闭" : "开启";
      if (/合并相似/.test(t)) f.alertMerge = /不合并/.test(t) ? "不合并" : "合并";
      if (/去重/.test(t)) f.alertDedup = /关|关闭/.test(t) ? "关闭" : "开启";
      if (/定时|每小时|1小时/.test(t)) {
        f.alertInterval = "定时预警";
        f.alertIntervalHours = "1小时";
      }
      if (/按标题/.test(t)) f.alertMatch = "按标题";
      else if (/按正文/.test(t)) f.alertMatch = "按正文";
    }
  }
  if (/方案名|名称|改名叫|改名为/.test(t)) {
    const m = t.match(/(?:叫|为|成)\s*[「"']?([^「」"'\s]{1,12})/);
    if (m) f.name = m[1].slice(0, 6);
  }
  if (/方案组|分组|放到|归属/.test(t)) {
    if (/文化/.test(t)) f.group = "中国文化";
    else if (/汽车|新能源/.test(t)) f.group = "汽车产业";
    else if (/国际|关税/.test(t)) f.group = "国际经贸";
  }
  if (/关键词|主体|监测词/.test(t) && (/改成|改为|换成|设置/.test(t) || !isPatch)) {
    const m = t.match(/(?:改成|改为|换成|设置[为成]?)\s*(.+)$/);
    if (m) f.keywords = cleanKeywordExpr(m[1]);
  }

  if (!isPatch || !f.keywords) {
    // initial fill from natural language
    if (/小米|SU7/i.test(t)) {
      f.name = f.name || "SU7口碑";
      f.group = f.group || "汽车产业";
      f.keywords = f.keywords || "小米+SU7|(小米汽车+SU7)";
      f.exclude = f.exclude || "预约试驾|购车优惠";
    } else if (/比亚迪|降价/.test(t)) {
      f.name = f.name || "比亚迪价";
      f.group = "汽车产业";
      f.keywords = "比亚迪+(降价|调价|促销)";
      f.exclude = "二手车|经销商招聘";
    } else if (/华为|P80|发布会/.test(t)) {
      f.name = f.name || "华为发布";
      f.group = "科技数码";
      f.keywords = "华为+(P80|发布会)";
      f.exclude = "招聘|股价";
    } else if (/关税/.test(t)) {
      f.name = f.name || "美国关税";
      f.group = "国际经贸";
      f.keywords = "(美国+关税)|(对华+加征)|(出口管制)";
      f.exclude = "股市|行情|概念股";
    } else if (/文保|文化保护/.test(t)) {
      f.name = f.name || "文化保护";
      f.group = "中国文化";
      f.keywords = "文化保护|(非物质文化遗产)|(文物保护)";
      f.exclude = "旅游团购|酒店";
    } else if (!f.keywords) {
      f.name = f.name || t.slice(0, 6);
      f.keywords = t.replace(/\s+/g, "+").slice(0, 80);
    }
    if (/负面|风险|预警/.test(t)) {
      f.alert = true;
      f.alertName = f.alertName || f.name || "风险预警";
      f.alertWords = f.alertWords || "异响,投诉,维权,召回";
      f.alertContent = "敏感";
      f.alertSources = ["全部"];
    }
  }
}

function cleanKeywordExpr(s) {
  return s
    .replace(/[「」""']/g, "")
    .replace(/^(把|将|请|帮我)/, "")
    .trim()
    .slice(0, 120);
}

function syncPlanFormFromDom() {
  const name = $("#plan-name");
  if (!name) return;
  state.planForm.name = name.value.trim();
  state.planForm.group = $("#plan-group").value;
  state.planForm.keywords = $("#plan-keywords").value;
  state.planForm.exclude = $("#plan-exclude").value;
  state.planForm.alert = $("#plan-alert").checked;
  if (state.planForm.alert) {
    state.planForm.alertName = $("#alert-name")?.value.trim() || "";
    state.planForm.alertWords = $("#alert-words")?.value.trim() || "";
    state.planForm.alertTime = $("#alert-time")?.value.trim() || "00:00 - 23:00";
    state.planForm.alertIntervalHours = $("#alert-interval-hours")?.value || "1小时";
    const sources = [...document.querySelectorAll("[data-alert-source].is-active")].map((el) => el.dataset.alertSource);
    if (sources.length) state.planForm.alertSources = sources;
    state.planForm.alertContent = $(".opt.is-active[data-alert-content]")?.dataset.alertContent || state.planForm.alertContent;
    state.planForm.alertMatch = $(".opt.is-active[data-alert-match]")?.dataset.alertMatch || state.planForm.alertMatch;
    state.planForm.alertChannel = $(".opt.is-active[data-alert-channel]")?.dataset.alertChannel || state.planForm.alertChannel;
    state.planForm.alertWeekend = $(".opt.is-active[data-alert-weekend]")?.dataset.alertWeekend || state.planForm.alertWeekend;
    state.planForm.alertMerge = $(".opt.is-active[data-alert-merge]")?.dataset.alertMerge || state.planForm.alertMerge;
    state.planForm.alertDedup = $(".opt.is-active[data-alert-dedup]")?.dataset.alertDedup || state.planForm.alertDedup;
    state.planForm.alertInterval = $(".opt.is-active[data-alert-interval]")?.dataset.alertInterval || state.planForm.alertInterval;
  }
}

function optGroup(attr, options, current, multi = false) {
  const selected = multi ? current || [] : [current];
  return options
    .map((opt) => {
      const on = selected.includes(opt);
      return `<button type="button" class="opt ${on ? "is-active" : ""}" data-${attr}="${opt}">${opt}</button>`;
    })
    .join("");
}

function render() {
  const scrollMap = {};
  document.querySelectorAll("[data-scroll-key]").forEach((el) => {
    scrollMap[el.dataset.scrollKey] = el.scrollTop;
  });

  document.querySelectorAll(".topnav__item").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.module === state.module);
  });

  const shell = $("#shell");
  shell.className = "shell";
  if (
    state.module === "overview" ||
    state.module === "search" ||
    state.module === "assistant" ||
    state.module === "settings" ||
    (state.module === "monitor" && state.monitorView === "create") ||
    (state.module === "event" && state.eventMode === "create")
  ) {
    shell.classList.add("shell--nosider");
  }

  if (state.module === "overview") shell.innerHTML = renderOverview();
  else if (state.module === "monitor") shell.innerHTML = renderMonitor();
  else if (state.module === "search") shell.innerHTML = renderSearch();
  else if (state.module === "event") shell.innerHTML = renderEvent();
  else if (state.module === "assistant") shell.innerHTML = renderAssistant();
  else if (state.module === "settings") shell.innerHTML = renderSettings();

  bind();
  if (state.modal) openModal(state.modal);

  document.querySelectorAll("[data-scroll-key]").forEach((el) => {
    const key = el.dataset.scrollKey;
    if (scrollMap[key] != null) el.scrollTop = scrollMap[key];
  });
}

function renderOverview() {
  const ov = overviewData || {
    total: 0, today_add: 0, alert_count: 0, positive_ratio: 0,
    emotion: { pos: 0, neu: 0, neg: 0 }, trend_7d: [], attention: [],
  };
  const emo = ov.emotion;
  const emoTotal = Math.max(1, (emo.pos || 0) + (emo.neu || 0) + (emo.neg || 0));
  const negPct = Math.round(((emo.neg || 0) / emoTotal) * 100);
  const trend = ov.trend_7d && ov.trend_7d.length ? ov.trend_7d : [0];
  const maxV = Math.max(1, ...trend);
  const pts = trend
    .map((v, i) => `${((i / Math.max(1, trend.length - 1)) * 400).toFixed(0)},${(128 - (v / maxV) * 96).toFixed(0)}`)
    .join(" ");
  const pPct = Math.round(((emo.pos || 0) / emoTotal) * 100);
  const nPct = Math.round(((emo.neu || 0) / emoTotal) * 100);
  const gPct = 100 - pPct - nPct;
  const donutBg = `conic-gradient(#3d8f6a 0 ${pPct}%, #c9a227 ${pPct}% ${pPct + nPct}%, #c44b4b ${pPct + nPct}% 100%)`;
  return `
  <main class="main" data-scroll-key="overview-main">
    <div class="hero-line">
      <div>
        <h1>早上好，今日舆情整体${negPct >= 30 ? "偏负面，请留意预警项" : negPct >= 15 ? "平稳，个别负面需关注" : "平稳"}</h1>
        <p class="muted">工作台汇总全站监测数据（来源：IT之家 / 少数派 / 百度热搜，自动采集与情感判定）；详细筛查请进入「主题监测」，独立检索请用「全文搜索」。</p>
      </div>
      <button type="button" class="btn-secondary" data-go="monitor">进入主题监测</button>
    </div>
    <div class="kpi-row">
      <div class="panel kpi"><div class="kpi__label">信息总量</div><div class="kpi__value">${ov.total.toLocaleString()}</div><div class="kpi__delta up">累计采集</div></div>
      <div class="panel kpi"><div class="kpi__label">今日新增</div><div class="kpi__value">${ov.today_add.toLocaleString()}</div><div class="kpi__delta up">今日采集</div></div>
      <div class="panel kpi"><div class="kpi__label">负面信息</div><div class="kpi__value">${(emo.neg || 0).toLocaleString()}</div><div class="kpi__delta warn">负面占比 ${negPct}%</div></div>
      <div class="panel kpi"><div class="kpi__label">正面占比</div><div class="kpi__value">${ov.positive_ratio}%</div><div class="kpi__delta up">情感自动判定</div></div>
    </div>
    <div class="grid-2">
      <section class="panel">
        <header class="panel__head"><h3>近 7 日舆情走势</h3><span class="muted">全站</span></header>
        <div class="panel__body">
          <div class="chart-line">
            <svg viewBox="0 0 400 140" preserveAspectRatio="none">
              <polyline fill="none" stroke="#1f6b5c" stroke-width="3" points="${pts}" />
            </svg>
          </div>
        </div>
      </section>
      <section class="panel">
        <header class="panel__head"><h3>情感分布</h3></header>
        <div class="panel__body">
          <div class="donut-wrap">
            <div class="donut" style="background:${donutBg}"></div>
            <div class="legend">
              <div class="p">正面 ${pPct}%</div>
              <div class="n">中性 ${nPct}%</div>
              <div class="g">负面 ${gPct}%</div>
            </div>
          </div>
        </div>
      </section>
    </div>
    <section class="panel">
      <header class="panel__head">
        <h3>需关注信息</h3>
        <button type="button" class="btn-ghost" data-go="monitor">查看更多</button>
      </header>
      <div class="panel__body">
        <ul class="attn-list">
          ${
            ov.attention.length
              ? ov.attention
                  .map(
                    (a) => `<li data-go="monitor" data-topic="${a.topic}">
                <i class="dot dot--neg"></i>
                <div>
                  <div class="attn__title">${escapeHtml(a.title)}</div>
                  <div class="attn__meta">${escapeHtml(a.meta)}</div>
                </div>
              </li>`
                  )
                  .join("")
              : `<li><div class="attn__title">暂无负面信息</div></li>`
          }
        </ul>
      </div>
    </section>
    <section class="panel">
      <header class="panel__head">
        <h3>情感态度归纳（AI 六维「情感与态度」）</h3>
        <button type="button" class="btn-primary" id="btn-insight">${state.insightLoading ? "归纳中…" : "生成归纳"}</button>
      </header>
      <div class="panel__body">${insightHtml(state.insightResult, state.insightLoading)}</div>
    </section>
    <section class="panel">
      <header class="panel__head">
        <h3>数据与情感管理</h3>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
          <button type="button" class="btn-secondary" id="btn-refresh-data">采集最新舆情数据</button>
          <button type="button" class="btn-primary" id="btn-recompute">${state.recomputing ? "计算中…" : "重新计算全部文章情感"}</button>
        </div>
      </header>
      <div class="panel__body">${resultHtml(state.sentimentResult)}</div>
    </section>
  </main>`;
}

function renderMonitor() {
  if (state.monitorView === "create") {
    return renderPlanCreate();
  }
  const topic = currentTopic();
  const list = filteredArticles();
  return `
  <aside class="sider">
    <button type="button" class="sider__btn" id="btn-new-topic">+ 新建监测主题</button>
    <button type="button" class="sider__btn secondary" id="btn-assist-topic">用智能助手配置</button>
    <div class="sider__title">我的主题</div>
    ${topics
      .map((t) => {
        const locked = state.sixdimLoading && t.id !== state.topicId;
        return `<button type="button" class="sider-item ${t.id === state.topicId ? "is-active" : ""} ${locked ? "is-disabled" : ""}" data-topic="${t.id}" ${locked ? 'aria-disabled="true" title="六维分析生成中，请稍候再切换"' : ""}>
        <div class="sider-item__name">${escapeHtml(t.name)}</div>
        <div class="sider-item__meta">${escapeHtml(t.meta)}</div>
      </button>`;
      })
      .join("")}
  </aside>
  <main class="main">
    <div class="toolbar">
      <div class="crumbs">主题监测 / <strong>${escapeHtml(topic.name)}</strong>
        <span class="muted"> · 合并原「监测分析 / 数据监测 / 监测管理」</span>
      </div>
      <div class="view-switch">
        <button type="button" class="${state.monitorView === "list" ? "is-active" : ""}" data-view="list">数据列表</button>
        <button type="button" class="${state.monitorView === "insight" ? "is-active" : ""}" data-view="insight">分析洞察</button>
        <button type="button" class="${state.monitorView === "manage" ? "is-active" : ""}" data-view="manage">方案管理</button>
      </div>
    </div>
    ${
      state.monitorView === "manage"
        ? renderManage(topic)
        : state.monitorView === "insight"
          ? renderSixLayerInsight(topic.name, "monitor")
          : `
    <section class="panel filter-box">
      <div class="filter-row">
        <div class="filter-label">时间</div>
        ${chipGroup("time", [
          ["24h", "24小时"],
          ["today", "今天"],
          ["7d", "7天"],
          ["30d", "30天"],
        ])}
      </div>
      <div class="filter-row">
        <div class="filter-label">媒体</div>
        ${chipGroup("media", [
          ["all", "全部"],
          ["news", "新闻"],
          ["hot", "热搜"],
        ])}
      </div>
      <div class="filter-row">
        <div class="filter-label">情感</div>
        ${chipGroup("sentiment", [
          ["all", "全部"],
          ["pos", "正面"],
          ["neu", "中性"],
          ["neg", "负面"],
        ])}
      </div>
    </section>
    <div class="content-split">
      <section class="panel list-panel">
        <header class="panel__head">
          <h3>监测结果</h3>
          <span class="muted">共 ${list.length} 条</span>
        </header>
        ${
          list.length
            ? list
                .map(
                  (a) => `<button type="button" class="article" data-article="${a.id}">
              <div class="article__title">${escapeHtml(a.title)}</div>
              <p class="article__summary">${escapeHtml(a.summary)}</p>
              <div class="article__meta">
                <span>${escapeHtml(a.source)}</span>
                <span>${escapeHtml(a.time)}</span>
                <span class="tag-mini ${a.sentiment}">${sentLabel(a.sentiment)}</span>
              </div>
            </button>`
                )
                .join("")
            : `<div class="panel__body"><div class="empty-block">当前筛选条件下暂无数据，请调整筛选或换主题</div></div>`
        }
      </section>
      <div>
        <section class="panel side-card">
          <h4>热词</h4>
          <ul class="rank">
            ${
              hotWords.length
                ? hotWords
                    .slice(0, 5)
                    .map((h, i) => `<li data-hot="${h.word}"><span class="idx">${i + 1}</span>${h.word}</li>`)
                    .join("")
                : `<li><span class="idx">-</span>暂无</li>`
            }
          </ul>
        </section>
        <section class="panel side-card">
          <h4>本主题摘要</h4>
          <p class="muted" style="margin:0 0 10px;line-height:1.55;font-size:13px;">负面集中在产品体验投诉；完整六维请切到「分析洞察」。</p>
          <button type="button" class="btn-secondary" data-view="insight" style="width:100%">打开分析洞察</button>
        </section>
      </div>
    </div>`
    }
  </main>`;
}

function renderPlanCreate() {
  const f = state.planForm;
  const emptyChat = !state.planChat.length;
  return `
  <main class="main main--plan">
    <div class="plan-banner">什么是监测方案：监测方案是与您相关或您关注的词条；通过设置词条，系统将把互联网中相关信息第一时间汇总给您。新手可先在右侧告诉助手需求，由表单自动填写。</div>
    <div class="plan-layout">
      <section class="panel plan-form-panel">
        <header class="panel__head">
          <h3>高级创建</h3>
          <span class="muted">当前是高级创建，可设置关键词组合</span>
        </header>
        <div class="plan-form" data-scroll-key="plan-form">
          <div class="plan-row">
            <label>方案名称</label>
            <div class="plan-field">
              <input id="plan-name" maxlength="6" placeholder="请输入方案名称" value="${escapeHtml(f.name)}" />
              <span class="plan-hint">*方案名称控制在 6 字符以内</span>
            </div>
          </div>
          <div class="plan-row">
            <label>所属方案组</label>
            <div class="plan-field">
              <select id="plan-group">
                ${["中国文化", "汽车产业", "科技数码", "国际经贸"]
                  .map((g) => `<option value="${g}" ${f.group === g ? "selected" : ""}>${g}</option>`)
                  .join("")}
              </select>
            </div>
          </div>
          <div class="plan-row plan-row--top">
            <label>方案主体关键词</label>
            <div class="plan-field">
              <textarea id="plan-keywords" rows="5" placeholder="示例：北京|上海|广州  或  上海+(世博会|世博)">${escapeHtml(f.keywords)}</textarea>
              <p class="plan-tip">+ 表示并且，| 表示或者；可用 ( ) 组合。助手填写后你仍可直接修改。</p>
            </div>
          </div>
          <div class="plan-row plan-row--top">
            <label>监测屏蔽歧义词</label>
            <div class="plan-field">
              <textarea id="plan-exclude" rows="4" placeholder="不希望出现的词，语法同关键词">${escapeHtml(f.exclude)}</textarea>
            </div>
          </div>
          <div class="plan-row">
            <label>预警开关</label>
            <div class="plan-field plan-field--switch">
              <label class="switch">
                <input type="checkbox" id="plan-alert" ${f.alert ? "checked" : ""} />
                <span class="switch__ui"></span>
              </label>
              <span class="alert-onoff ${f.alert ? "is-on" : ""}">${f.alert ? "ON" : "OFF"}</span>
            </div>
          </div>
          ${
            f.alert
              ? `<div class="alert-box">
            <div class="alert-box__title">预警配置</div>
            <div class="plan-row">
              <label>预警名称</label>
              <div class="plan-field">
                <input id="alert-name" placeholder="请输入预警名称" value="${escapeHtml(f.alertName)}" />
              </div>
            </div>
            <div class="plan-row plan-row--top">
              <label>设置预警词</label>
              <div class="plan-field">
                <textarea id="alert-words" rows="3" placeholder="请输入预警词，用逗号隔开">${escapeHtml(f.alertWords)}</textarea>
              </div>
            </div>
            <div class="alert-line alert-line--sources">
              <span>来源类型</span>
              <div class="opt-wrap">
                ${optGroup(
                  "alert-source",
                  ["全部", "微信", "微博", "政务", "论坛", "新闻", "报刊", "客户端", "网站", "外媒", "视频", "博客"],
                  f.alertSources,
                  true
                )}
              </div>
            </div>
            <div class="alert-grid">
              <div>
                <div class="alert-line"><span>预警内容</span><div class="opt-wrap">${optGroup("alert-content", ["全部", "敏感"], f.alertContent)}</div></div>
                <div class="alert-line"><span>匹配方式</span><div class="opt-wrap">${optGroup("alert-match", ["全文", "按标题", "按正文"], f.alertMatch)}</div></div>
                <div class="alert-line"><span>预警来源</span><div class="opt-wrap">${optGroup("alert-channel", ["系统推送", "邮箱推送", "公众号推送"], f.alertChannel)}</div></div>
                <div class="alert-line"><span>周末预警</span><div class="opt-wrap">${optGroup("alert-weekend", ["开启", "关闭"], f.alertWeekend)}</div></div>
              </div>
              <div>
                <div class="alert-line"><span>相似文章合并</span><div class="opt-wrap">${optGroup("alert-merge", ["合并", "不合并"], f.alertMerge)}</div></div>
                <div class="alert-line"><span>预警去重</span><div class="opt-wrap">${optGroup("alert-dedup", ["开启", "关闭"], f.alertDedup)}</div></div>
                <div class="alert-line"><span>接收时间</span><div class="plan-field"><input id="alert-time" value="${escapeHtml(f.alertTime)}" /></div></div>
                <div class="alert-line"><span>预警间隔</span>
                  <div class="opt-wrap opt-wrap--interval">
                    ${optGroup("alert-interval", ["实时预警", "定时预警"], f.alertInterval)}
                    <select id="alert-interval-hours" ${f.alertInterval === "定时预警" ? "" : "disabled"}>
                      ${["1小时", "2小时", "6小时", "12小时"]
                        .map((h) => `<option ${f.alertIntervalHours === h ? "selected" : ""}>${h}</option>`)
                        .join("")}
                    </select>
                  </div>
                </div>
              </div>
            </div>
          </div>`
              : ""
          }
          <div class="plan-actions">
            <button type="button" class="btn-primary" id="btn-plan-save">保存</button>
            <button type="button" class="btn-secondary" id="btn-plan-cancel">取消</button>
          </div>
        </div>
      </section>

      <section class="panel plan-agent-panel">
        <header class="panel__head">
          <h3>智能助手填表</h3>
          <span class="tag-mini">可改字段 / 可对话修改</span>
        </header>
        <div class="chat" id="plan-chat" data-scroll-key="plan-chat">
          ${
            emptyChat
              ? `<div class="empty-hero">
                  <h2>用一句话说清要监测什么</h2>
                  <p>助手会填写左侧表单。填完后你可以：① 直接改表单；② 继续说「屏蔽词加上房产」「预警打开」等。</p>
                  <div class="examples">
                    <button type="button" class="chip" data-plan-fill="监控小米 SU7 近 7 天口碑，重点看负面，屏蔽预约试驾">监控小米 SU7 口碑，重点负面</button>
                    <button type="button" class="chip" data-plan-fill="建一个中国文化里文化保护相关监测，排除旅游团购">文化保护监测，排除团购</button>
                    <button type="button" class="chip" data-plan-fill="关注美国关税对华科技影响，打开预警">美国关税，打开预警</button>
                  </div>
                </div>`
              : state.planChat
                  .map((m) =>
                    m.role === "user"
                      ? `<div class="bubble bubble--user">${escapeHtml(m.text)}</div>`
                      : `<div class="bubble bubble--agent">${m.html}</div>`
                  )
                  .join("")
          }
        </div>
        <div class="composer">
          <input id="plan-agent-input" placeholder="例如：把屏蔽词改成 房产|招聘，并打开预警" />
          <button type="button" class="btn-primary" id="btn-plan-agent-send">发送</button>
        </div>
      </section>
    </div>
  </main>`;
}

function renderManage(topic) {
  return `
  <section class="panel">
    <header class="panel__head"><h3>方案管理</h3><span class="muted">原监测管理能力收拢于此</span></header>
    <div class="panel__body">
      <table class="table">
        <thead>
          <tr><th>主题</th><th>状态</th><th>数据源</th><th>操作</th></tr>
        </thead>
        <tbody>
          ${topics
            .map(
              (t) => `<tr>
            <td>${escapeHtml(t.name)}</td>
            <td><span class="status-pill">${escapeHtml(t.status)}</span></td>
            <td>${escapeHtml(t.meta)}</td>
            <td>
              <button type="button" class="btn-ghost" data-topic="${t.id}" data-jump-list>查看数据</button>
              <button type="button" class="btn-ghost" data-edit-topic="${t.id}">编辑</button>
            </td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <p class="muted" style="margin-top:12px;">说明：不再单独设立「监测分析 / 数据监测 / 监测管理」三个顶栏；列表与方案管理都是同一主题数据的不同视图。</p>
    </div>
  </section>`;
}

function chipGroup(key, items) {
  return items
    .map(
      ([val, label]) =>
        `<button type="button" class="chip ${state.filters[key] === val ? "is-active" : ""}" data-filter="${key}" data-value="${val}">${label}</button>`
    )
    .join("");
}

function sentLabel(s) {
  return { pos: "正面", neu: "中性", neg: "负面" }[s] || s;
}

function renderSearch() {
  const results = state.searchDone
    ? articles.filter((a) => !state.searchQ || a.title.includes(state.searchQ) || a.summary.includes(state.searchQ))
    : [];
  return `
  <main class="main">
    <div class="hero-line">
      <div>
        <h1>全文搜索</h1>
        <p class="muted">不依赖智能助手，可独立检索全网舆情信息。</p>
      </div>
    </div>
    <div class="search-hero">
      <input id="search-input" type="text" placeholder="输入企业、产品、人物、政策等关键词" value="${escapeHtml(state.searchQ)}" />
      <button type="button" class="btn-primary" id="btn-search">全文搜索</button>
    </div>
    <section class="panel filter-box">
      <div class="filter-row">
        <div class="filter-label">时间</div>
        ${chipGroup("time", [
          ["24h", "24小时"],
          ["today", "今天"],
          ["7d", "7天"],
          ["30d", "30天"],
        ])}
      </div>
      <div class="filter-row">
        <div class="filter-label">媒体</div>
        ${chipGroup("media", [
          ["all", "全部"],
          ["news", "新闻"],
          ["hot", "热搜"],
        ])}
      </div>
    </section>
    <div class="content-split">
      <section class="panel list-panel">
        <header class="panel__head">
          <h3>${state.searchDone ? "搜索结果" : "搜索记录"}</h3>
          <span class="muted">${state.searchDone ? `约 ${results.length} 条` : "最近"}</span>
        </header>
        ${
          !state.searchDone
            ? `<div class="panel__body">
                <ul class="rank">
                  <li data-history="小米 SU7"><span class="idx">1</span>小米 SU7</li>
                  <li data-history="武汉大学"><span class="idx">2</span>武汉大学</li>
                  <li data-history="美国关税"><span class="idx">3</span>美国关税</li>
                </ul>
              </div>`
            : results.length
              ? results
                  .map(
                    (a) => `<button type="button" class="article" data-article="${a.id}">
                <div class="article__title">${escapeHtml(a.title)}</div>
                <p class="article__summary">${escapeHtml(a.summary)}</p>
                <div class="article__meta"><span>${escapeHtml(a.source)}</span><span>${escapeHtml(a.time)}</span></div>
              </button>`
                  )
                  .join("")
              : `<div class="panel__body"><div class="empty-block">当前暂无数据，请更换关键词</div></div>`
        }
      </section>
      <section class="panel side-card">
        <h4>热词榜</h4>
        <ul class="rank">
          ${
            hotWords.length
              ? hotWords
                  .slice(0, 5)
                  .map((h, i) => `<li data-history="${h.word}"><span class="idx">${i + 1}</span>${h.word}</li>`)
                  .join("")
              : `<li><span class="idx">-</span>暂无</li>`
          }
        </ul>
      </section>
    </div>
  </main>`;
}

function openEventCreate(seedText) {
  state.module = "event";
  state.eventMode = "create";
  state.eventForm = emptyEventForm();
  state.eventChat = [];
  if (seedText) {
    state.eventChat.push({ role: "user", text: seedText });
    applyEventFill(seedText, false);
    state.eventChat.push({
      role: "agent",
      html: `<p>已根据你的描述填写左侧「创建事件分析任务」表单。可直接改字段，或继续告诉我要改哪一项。</p>`,
    });
  }
  render();
}

function applyEventFill(text, isPatch) {
  const f = state.eventForm;
  const t = text.trim();

  if (/屏蔽|排除/.test(t)) {
    const add = t.replace(/^.*?(加上|改为|改成|设置[为成]?)/, "").trim() || t;
    if (/清空/.test(t)) f.exclude = "";
    else if (/改成|改为/.test(t)) f.exclude = cleanKeywordExpr(add);
    else f.exclude = f.exclude ? `${f.exclude}|${cleanKeywordExpr(add)}` : cleanKeywordExpr(add);
  }
  if (/涉及词|关键词/.test(t) && (/改成|改为|换成|设置/.test(t) || !isPatch)) {
    const m = t.match(/(?:改成|改为|换成|设置[为成]?)\s*(.+)$/);
    if (m) f.keywords = cleanKeywordExpr(m[1]);
  }
  if (/任务名|名称|改名叫|改名为/.test(t)) {
    const m = t.match(/(?:叫|为|成)\s*[「"']?([^「」"'\s]{1,20})/);
    if (m) f.name = m[1].slice(0, 20);
  }
  if (/近\s*7\s*天|一周/.test(t)) {
    f.start = "2026/08/05";
    f.end = "2026/08/12";
  } else if (/近\s*14\s*天|两周/.test(t)) {
    f.start = "2026/07/29";
    f.end = "2026/08/12";
  } else if (/近\s*一个?月|30\s*天/.test(t)) {
    f.start = "2026/07/12";
    f.end = "2026/08/12";
  }

  if (!isPatch || !f.keywords) {
    if (/关税/.test(t)) {
      f.name = f.name || "美国关税";
      f.keywords = f.keywords || "(美国+关税)|(对华+加征)|(出口管制)";
      f.exclude = f.exclude || "股市|行情|概念股";
      f.start = "2026/07/12";
      f.end = "2026/08/12";
    } else if (/召回/.test(t)) {
      f.name = f.name || "品牌召回";
      f.keywords = f.keywords || "(召回)+(缺陷|安全|投诉)";
      f.exclude = f.exclude || "广告|软文|促销";
      f.start = "2026/07/29";
      f.end = "2026/08/12";
    } else if (/新政|地方/.test(t)) {
      f.name = f.name || "地方新政";
      f.keywords = f.keywords || "(政策|新政)+(发布|落地)";
      f.exclude = f.exclude || "招聘";
    } else if (!f.keywords) {
      f.name = f.name || t.slice(0, 12);
      f.keywords = t.replace(/\s+/g, "+").slice(0, 100);
    }
  }
}

function syncEventFormFromDom() {
  if (!$("#event-name")) return;
  state.eventForm.name = $("#event-name").value.trim();
  state.eventForm.start = $("#event-start").value.trim();
  state.eventForm.end = $("#event-end").value.trim();
  state.eventForm.keywords = $("#event-keywords").value;
  state.eventForm.exclude = $("#event-exclude").value;
}

function renderEvent() {
  if (state.eventMode === "create") {
    return renderEventCreate();
  }
  if (state.eventMode === "result") {
    return renderEventResult();
  }

  return `
  <aside class="sider">
    <button type="button" class="sider__btn" id="btn-new-event">+ 创建事件分析</button>
    <div class="sider__title">任务</div>
    ${events
      .map(
        (e) => `<button type="button" class="sider-item" data-open-event="${e.id}">
        <div class="sider-item__name">${escapeHtml(e.name)}</div>
        <div class="sider-item__meta">${escapeHtml(e.status)} · ${escapeHtml(e.createdAt)}</div>
      </button>`
      )
      .join("")}
  </aside>
  <main class="main">
    <div class="toolbar">
      <div class="crumbs">事件分析 / <strong>任务列表</strong></div>
    </div>
    <section class="panel">
      <header class="panel__head"><h3>事件分析任务</h3><span class="muted">共 ${events.length} 个</span></header>
      <div class="panel__body">
        <table class="table">
          <thead><tr><th>事件名称</th><th>创建时间</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            ${events
              .map(
                (e) => `<tr>
              <td>${escapeHtml(e.name)}</td>
              <td>${escapeHtml(e.createdAt)}</td>
              <td><span class="status-pill">${escapeHtml(e.status)}</span></td>
              <td><button type="button" class="btn-ghost" data-open-event="${e.id}">查看</button></td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </section>
  </main>`;
}

async function refreshData() {
  toast("正在采集最新舆情数据…");
  try {
    const r = await fetch("/api/collect", { method: "POST" }).then((r) => r.json());
    const d = r.data || {};
    if (d.ok) {
      toast(`采集完成：新增 ${d.new_count} 条，共 ${d.total} 条`);
      loadData();
      loadSentimentResult();
      loadMetrics();
    } else {
      toast("采集失败：" + (d.error || "未知原因"));
    }
  } catch (e) {
    toast("采集失败：数据服务未运行");
  }
}

async function loadMetrics() {
  const scope = state.sixdimScope ? "?scope=" + encodeURIComponent(state.sixdimScope) : "";
  try {
    const r = await fetch("/api/metrics" + scope).then((r) => r.json());
    state.metricsData = r.data || null;
  } catch (e) {
    state.metricsData = null;
  }
  // 加载完成后刷新，让图表显示真实数据
  if ((state.module === "monitor" && state.monitorView === "insight") ||
      (state.module === "event" && state.eventMode === "result")) {
    render();
  }
}

function renderMiniCharts(m) {
  const trend = (m && m.trend) || [];
  const emo = (m && m.emotion) || { pos: 0, neu: 0, neg: 0, total: 0, pos_ratio: 0, neu_ratio: 0, neg_ratio: 0 };
  const source = (m && m.source) || [];
  const negPct = emo.neg_ratio || 0;
  const neuPct = emo.neu_ratio || 0;
  const posPct = emo.pos_ratio || 0;
  const maxV = Math.max(1, ...trend.map((t) => t.count));
  const pts = trend.map((t, i) => `${(i / Math.max(1, trend.length - 1)) * 280 + 10},${55 - (t.count / maxV) * 40}`).join(" ");
  const trendSvg = trend.length
    ? `<svg class="sparkline" viewBox="0 0 300 60" preserveAspectRatio="none"><polyline fill="none" stroke="#059669" stroke-width="2" points="${pts}"/></svg>`
    : `<div class="muted">暂无数据</div>`;
  const emotionDonut = `<svg width="72" height="72" viewBox="0 0 72 72">
    <circle cx="36" cy="36" r="30" fill="none" stroke="#e5e7eb" stroke-width="10"/>
    <circle cx="36" cy="36" r="30" fill="none" stroke="#fca5a5" stroke-width="10" stroke-dasharray="${(negPct / 100) * 188.5} 188.5" transform="rotate(-90 36 36)"/>
    <circle cx="36" cy="36" r="30" fill="none" stroke="#fcd34d" stroke-width="10" stroke-dasharray="${(neuPct / 100) * 188.5} 188.5" stroke-dashoffset="${-(negPct / 100) * 188.5}" transform="rotate(-90 36 36)"/>
    <circle cx="36" cy="36" r="30" fill="none" stroke="#86efac" stroke-width="10" stroke-dasharray="${(posPct / 100) * 188.5} 188.5" stroke-dashoffset="${-((negPct + neuPct) / 100) * 188.5}" transform="rotate(-90 36 36)"/>
  </svg>`;
  const srcTotal = Math.max(1, source.reduce((s, x) => s + x.count, 0));
  const sourceBars = source.map((s) => `
    <div class="src-row">
      <span class="src-label">${escapeHtml(s.source)}</span>
      <div class="src-track"><div class="src-fill" style="width:${Math.round((s.count / srcTotal) * 100)}%"></div></div>
      <span class="src-val">${s.count}</span>
    </div>`).join("");
  return `
  <div class="mini-charts">
    <div class="chart-card">
      <div class="chart-card__header">
        <span class="chart-card__title">声量趋势（近7天）</span>
        <span class="chart-card__head-right"><span class="chart-card__value">${m ? m.total : 0}</span></span>
      </div>
      ${trendSvg}
      <div class="chart-card__foot">按发布时间真实统计 · 共 ${m ? m.total : 0} 条</div>
    </div>
    <div class="chart-card">
      <div class="chart-card__header">
        <span class="chart-card__title">情感分布</span>
        <span class="chart-card__sub">基于 ${emo.total} 条</span>
      </div>
      <div class="donut-wrap insight-donut">
        ${emotionDonut}
        <div class="donut-legend">
          <div class="legend-item"><span class="legend-dot" style="background:#fca5a5"></span>负面 ${emo.neg}（${negPct}%）</div>
          <div class="legend-item"><span class="legend-dot" style="background:#fcd34d"></span>中性 ${emo.neu}（${neuPct}%）</div>
          <div class="legend-item"><span class="legend-dot" style="background:#86efac"></span>正面 ${emo.pos}（${posPct}%）</div>
        </div>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-card__header">
        <span class="chart-card__title">来源构成</span>
        <span class="chart-card__sub">基于来源统计</span>
      </div>
      ${sourceBars || '<div class="muted">暂无数据</div>'}
      <div class="chart-card__foot">来源声量占比 · 完整传播链待接入平台数据</div>
    </div>
  </div>`;
}

function renderActions(acts) {
  const list = (acts && acts.length)
    ? acts.map((a) => {
        const p = a.priority === 1 ? "priority--1" : a.priority === 2 ? "priority--2" : "priority--3";
        return `<div class="action-item">
          <div class="action-item__left">
            <span class="action-priority ${p}"></span>
            <span class="action-text"><strong>${escapeHtml(a.title)}</strong> — ${escapeHtml(a.detail)}</span>
          </div>
        </div>`;
      }).join("")
    : `<div class="action-item"><div class="action-item__left"><span class="action-text muted">生成六维分析后，这里会显示模型给出的建议优先行动。</span></div></div>`;
  return `
  <div class="action-panel">
    <div class="action-panel__title">建议优先行动</div>
    <div class="action-list">${list}</div>
  </div>`;
}

function mdToHtml(md) {
  const lines = (md || "").split("\n");
  let html = "";
  let inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith("### ")) { closeList(); html += `<h4>${escapeHtml(t.slice(4))}</h4>`; }
    else if (t.startsWith("## ")) { closeList(); html += `<h3>${escapeHtml(t.slice(3))}</h3>`; }
    else if (t.startsWith("# ")) { closeList(); html += `<h2>${escapeHtml(t.slice(2))}</h2>`; }
    else if (t.startsWith("- ") || t.startsWith("* ")) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${escapeHtml(t.slice(2))}</li>`; }
    else if (t === "") { closeList(); }
    else { closeList(); html += `<p>${escapeHtml(line)}</p>`; }
  }
  closeList();
  return html;
}

function openReportLoading(title, hint) {
  const old = document.getElementById("modal");
  if (old) old.remove();
  const wrap = document.createElement("div");
  wrap.className = "modal-mask";
  wrap.id = "modal";
  wrap.innerHTML = `
    <div class="modal modal--report">
      <h3>${escapeHtml(title || "舆情研判分析报告")}</h3>
      <div class="report-loading">
        <div class="spinner"></div>
        <p>正在调用大模型生成…</p>
        <p class="muted">${escapeHtml(hint || "完整文档通常需要数十秒至 1~2 分钟，请耐心等待，不要关闭页面。")}</p>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener("click", (e) => { if (e.target === wrap) wrap.remove(); });
}

function _reportHtml(md, title) {
  const docTitle = title || "舆情研判分析报告";
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>${docTitle}</title>
  <style>
    body { font-family: "Microsoft YaHei", "PingFang SC", "SimSun", sans-serif; line-height: 1.8; padding: 40px; max-width: 820px; margin: 0 auto; color: #1f2937; }
    h2 { font-size: 20px; border-bottom: 2px solid #1f6b5c; padding-bottom: 6px; margin-top: 28px; }
    h3 { font-size: 16px; margin-top: 20px; }
    h4 { font-size: 14px; margin-top: 14px; color: #374151; }
    p, li { font-size: 14px; }
    ul { padding-left: 20px; }
  </style></head><body>${mdToHtml(md)}</body></html>`;
}

function exportReportPdf(md, title) {
  try {
    const w = window.open("", "_blank");
    if (!w) { toast("请允许浏览器弹窗后重试"); return; }
    w.document.write(_reportHtml(md, title));
    w.document.close();
    w.focus();
    setTimeout(() => { w.print(); }, 400);
    toast("已打开打印窗口，选择「另存为 PDF」即可");
  } catch (e) {
    toast("导出 PDF 失败");
  }
}

function exportReportWord(md, title) {
  try {
    const scopeName = (state.sixdimScope || "all").replace(/[:]/g, "_");
    const docTitle = title || "舆情研判分析报告";
    const html = `<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word" xmlns="http://www.w3.org/TR/REC-html40"><head><meta charset="utf-8"><title>${docTitle}</title></head><body>${mdToHtml(md)}</body></html>`;
    const blob = new Blob(["\ufeff" + html], { type: "application/msword" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${docTitle}_${scopeName}_${new Date().toISOString().slice(0, 10)}.doc`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast("已导出 Word 文档");
  } catch (e) {
    toast("导出 Word 失败");
  }
}

function openReportModal(md, opts) {
  opts = opts || {};
  const title = opts.title || "舆情研判分析报告";
  const old = document.getElementById("modal");
  if (old) old.remove();
  const wrap = document.createElement("div");
  wrap.className = "modal-mask";
  wrap.id = "modal";
  wrap.innerHTML = `
    <div class="modal modal--report">
      <h3>${escapeHtml(title)}</h3>
      <div class="report-content">${mdToHtml(md)}</div>
      <div class="modal__actions">
        <button type="button" class="btn-secondary" id="btn-report-copy">复制全文</button>
        <button type="button" class="btn-secondary" id="btn-report-pdf">导出 PDF</button>
        <button type="button" class="btn-secondary" id="btn-report-word">导出 Word</button>
        <button type="button" class="btn-secondary" id="btn-report-regen">重新生成</button>
        <button type="button" class="btn-primary" data-close-modal>关闭</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener("click", (e) => {
    if (e.target === wrap || e.target.matches("[data-close-modal]")) wrap.remove();
  });
  const copyBtn = wrap.querySelector("#btn-report-copy");
  if (copyBtn) copyBtn.onclick = () => {
    if (navigator.clipboard) navigator.clipboard.writeText(md).then(() => toast(opts.copyLabel || "已复制报告全文"));
    else toast("复制失败：浏览器不支持");
  };
  const pdfBtn = wrap.querySelector("#btn-report-pdf");
  if (pdfBtn) pdfBtn.onclick = () => exportReportPdf(md, title);
  const wordBtn = wrap.querySelector("#btn-report-word");
  if (wordBtn) wordBtn.onclick = () => exportReportWord(md, title);
  const regenBtn = wrap.querySelector("#btn-report-regen");
  if (regenBtn) regenBtn.onclick = () => {
    wrap.remove();
    if (opts.onRegen) opts.onRegen();
    else generateReport();
  };
}

function viewReport() {
  const scope = state.sixdimScope || "all";
  const cached = state.reportCache[scope];
  if (cached) openReportModal(cached);
  else generateReport();
}

async function generateReport() {
  state.reportLoading = true;
  openReportLoading();
  try {
    const scopeKey = state.sixdimScope || "all";
    const scope = state.sixdimScope ? "?scope=" + encodeURIComponent(state.sixdimScope) : "";
    const r = await fetch("/api/report" + scope, { method: "POST" }).then((r) => r.json());
    const d = r.data || {};
    const report = d.report || null;
    if (report) {
      state.reportData = report;
      state.reportCache[scopeKey] = report; // 缓存，区分重新生成/重新打开
      openReportModal(report);
      toast("报告已生成");
    } else {
      const old = document.getElementById("modal");
      if (old) old.remove();
      toast("报告生成失败：" + (d.error || r.msg || "未知原因"));
    }
  } catch (e) {
    const old = document.getElementById("modal");
    if (old) old.remove();
    toast("报告生成失败：数据服务未运行");
  }
  state.reportLoading = false;
}

const PLAN_OPTIONS = [
  { id: "daily", name: "日常/定期舆情应对方案", desc: "本周必做、苗头处置与升级准则（分析不超过三成）" },
  { id: "compete", name: "市场竞争格局应对方案", desc: "对标结论 + 反制清单与分情景预案（高风险须确认）" },
  { id: "crisis", name: "突发事件舆情应对方案", desc: "处置清单、分情景预案与沟通准则（高风险，确认后才生成）" },
];

function currentScopeName() {
  if (state.module === "event") {
    const ev = events.find((e) => e.id === state.eventId);
    return ev ? ev.name : "事件分析";
  }
  const t = currentTopic();
  return t ? t.name : "未命名主题";
}

function viewPlan() {
  if (state.sixdimLoading) {
    toast("六维分析生成中，请稍候再生成应对方案");
    return;
  }
  const scope = state.sixdimScope || "all";
  const cached = state.planCache[scope];
  if (cached && cached.md && !state.sixdimData) {
    openPlanDoc(cached);
    return;
  }
  if (!state.sixdimData) {
    toast("请先生成六维分析");
    return;
  }
  if (cached && cached.md) openPlanDoc(cached);
  else startPlanFlow();
}

async function startPlanFlow() {
  if (!state.sixdimData) {
    toast("请先生成六维分析");
    return;
  }
  try {
    const r = await fetch("/api/plan-route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope: state.sixdimScope || "",
        six_dimensions: state.sixdimData,
        topic_name: currentScopeName(),
      }),
    }).then((x) => x.json());
    const d = r.data || {};
    if (!d.template_id) {
      toast("模板推荐失败：" + (d.error || r.msg || "未知原因"));
      return;
    }
    openPlanConfirm(d);
  } catch (e) {
    toast("模板推荐失败：数据服务未运行");
  }
}

function openPlanConfirm(route) {
  const old = document.getElementById("modal");
  if (old) old.remove();
  const recommended = route.template_id || "daily";
  const reasons = (route.reasons || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
  const hint = route.template_id === "crisis"
    ? (route.hint || "危机模板须确认后才生成，生成结果须人工复核后对外使用")
    : (route.hint || "当前不像突发，若要写处置方案请改选危机模板");
  const options = PLAN_OPTIONS.map((o) => `
    <label class="plan-template-item">
      <input type="radio" name="plan-tpl" value="${o.id}" ${o.id === recommended ? "checked" : ""} />
      <div>
        <strong>${escapeHtml(o.name)}${o.id === recommended ? " · 规则推荐" : ""}</strong>
        <span>${escapeHtml(o.desc)}</span>
      </div>
    </label>`).join("");
  const wrap = document.createElement("div");
  wrap.className = "modal-mask";
  wrap.id = "modal";
  wrap.innerHTML = `
    <div class="modal modal--plan-confirm">
      <h3>选择应对方案模板</h3>
      <p>规则推荐「${escapeHtml(route.title || recommended)}」。请确认后再生成；详细数据分析请用「生成研判报告」。</p>
      <ul class="plan-reasons">${reasons}</ul>
      <p class="plan-confirm-hint">${escapeHtml(hint)}</p>
      <div class="plan-template-list">${options}</div>
      <div class="modal__actions">
        <button type="button" class="btn-secondary" data-close-modal>取消</button>
        <button type="button" class="btn-primary" id="btn-plan-confirm">确认并生成</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener("click", (e) => {
    if (e.target === wrap || e.target.matches("[data-close-modal]")) wrap.remove();
  });
  const ok = wrap.querySelector("#btn-plan-confirm");
  if (ok) ok.onclick = () => {
    const picked = wrap.querySelector("input[name='plan-tpl']:checked");
    const tid = picked ? picked.value : recommended;
    wrap.remove();
    generatePlan(tid);
  };
}

function openPlanDoc(rec) {
  openReportModal(rec.md, {
    title: rec.title || "应对方案",
    copyLabel: "已复制全文",
    onRegen: startPlanFlow,
  });
}

async function generatePlan(templateId) {
  state.planLoading = true;
  const meta = PLAN_OPTIONS.find((x) => x.id === templateId);
  const title = meta ? meta.name : "应对方案";
  openReportLoading(title, "按已确认模板生成，约需数十秒至一两分钟，请勿关闭页面。");
  try {
    const scopeKey = state.sixdimScope || "all";
    const r = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope: state.sixdimScope || "",
        template_id: templateId,
        six_dimensions: state.sixdimData,
        topic_name: currentScopeName(),
      }),
    }).then((x) => x.json());
    const d = r.data || {};
    const plan = d.plan || null;
    if (plan) {
      const rec = { md: plan, templateId: d.template_id || templateId, title: d.title || title };
      state.planCache[scopeKey] = rec;
      openPlanDoc(rec);
      toast("应对方案已生成，请人工复核后再对外使用");
    } else {
      const old = document.getElementById("modal");
      if (old) old.remove();
      toast("方案生成失败：" + (d.error || r.msg || "未知原因"));
    }
  } catch (e) {
    const old = document.getElementById("modal");
    if (old) old.remove();
    toast("方案生成失败：数据服务未运行");
  }
  state.planLoading = false;
}

async function generateSixdim() {
  const requestScope = state.sixdimScope;
  state.sixdimLoading = true;
  state.sixdimData = null;
  render();
  toast("正在调用大模型生成六维分析，约需数十秒…");
  try {
    const scope = requestScope ? "?scope=" + encodeURIComponent(requestScope) : "";
    const r = await fetch("/api/insight-sixdim" + scope, { method: "POST" }).then((r) => r.json());
    const d = r.data || {};
    const result = d.six_dimensions || null;
    if (result) {
      state.sixdimCache[requestScope] = result; // 缓存，切换页面不丢
      if (state.sixdimScope === requestScope) state.sixdimData = result;
      toast("六维分析已生成");
    } else {
      const err = d.error || r.msg || (r.code ? `接口返回 code=${r.code}` : "未知原因");
      toast("六维生成失败：" + err);
    }
  } catch (e) {
    toast("六维生成失败：数据服务未运行，请确认已重启 server.py");
  }
  state.sixdimLoading = false;
  render();
}

function renderSixdimCards(d) {
  const tagClass = (tag) => tag === "需关注" ? "tag--risk" : "tag--ai";
  const dim = (title, tag, rows) => `
    <div class="analysis-card">
      <div class="analysis-card__header">
        <span class="analysis-card__title">${title}</span>
        <span class="analysis-card__tag ${tagClass(tag)}">${tag}</span>
      </div>
      ${rows}
    </div>`;
  const row = (label, value, cls) => `
    <div class="metric-row">
      <span class="metric-label">${label}</span>
      <div class="metric-body">
        <div class="metric-value ${cls || ""}">${escapeHtml(value || "暂无足够数据")}</div>
      </div>
    </div>`;

  const b = d.basic || {};
  const e = d.emotion || {};
  const n = d.narrative || {};
  const s = d.spread || {};
  const beh = d.behavior || {};
  const deep = d.deep_impact || {};
  const st = e.stance_split || {};
  const stA = st["支持"] || 0;
  const stB = st["反对"] || 0;
  const stC = st["中立"] || 0;
  const stD = st["理中客"] || 0;
  const stTotal = (stA + stB + stC + stD) || 1;
  const pct = (v) => Math.round((v / stTotal) * 100);

  // 立场分化：彩色分段条 + 标签（参考根目录六维效果）
  const stanceBlock = `
    <div class="metric-row">
      <span class="metric-label">立场分化</span>
      <div class="metric-body">
        <div class="stance-bar">
          <div class="stance-seg stance-seg--a" style="width:${pct(stB)}%"></div>
          <div class="stance-seg stance-seg--b" style="width:${pct(stC)}%"></div>
          <div class="stance-seg stance-seg--c" style="width:${pct(stA)}%"></div>
          <div class="stance-seg stance-seg--d" style="width:${pct(stD)}%"></div>
        </div>
        <div class="stance-labels">
          <span>反对 ${stB}%</span><span>中立 ${stC}%</span><span>支持 ${stA}%</span><span>理中客 ${stD}%</span>
        </div>
      </div>
    </div>`;

  return `
  <div class="analysis-grid">
    ${dim("1. 基础信息", "AI 提取",
      row("声量", b.volume, "metric-value--strong") + row("发声画像", b.voice_profile) + row("表达方式", b.expression))}
    ${dim("2. 情感与态度", "需关注",
      row("情感性质", e.sentiment) + row("态度强度", e.attitude_strength) +
      stanceBlock + row("情感迁移", e.emotion_shift, "metric-value--alert"))}
    ${dim("3. 叙事与框架", "AI 归纳",
      row("议题框架", n.issue_frame) + row("符号隐喻", n.symbol_metaphor) + row("归因", n.attribution) + row("诉求", n.appeal))}
    ${dim("4. 传播结构", "AI 提取",
      row("传播路径", s.path) + row("意见领袖", s.kols) + row("平台情况", s.platform) + row("是否反转", s.reversal))}
    ${dim("5. 行为倾向", "AI 研判",
      row("线下行动", beh.offline_action) + row("消费影响", beh.consumption) + row("制度化参与", beh.institutional) + row("信息搜寻", beh.info_seeking))}
    ${dim("6. 深层影响", "AI 归纳",
      row("社会情绪", deep.social_emotion) + row("群体差异", deep.group_diff) + row("价值观冲突", deep.value_conflict) + row("历史类比", deep.historical_analogy))}
  </div>`;
}

/** 六维舆情分析驾驶舱（监测洞察 / 事件报告共用；全文搜索不做） */
function renderSixLayerInsight(scopeName, mode) {
  const title = `${escapeHtml(scopeName)} · 六维舆情分析`;
  const meta =
    mode === "event"
      ? "事件分析报告 · 指标基于真实数据统计，六维由大模型归纳生成"
      : "主题监测洞察 · 指标基于真实数据统计，六维由大模型归纳生成";
  const sixdimBody = state.sixdimLoading
    ? `<div class="sixdim-loading">正在调用大模型生成六维分析，请稍候…（约需数十秒）</div>`
    : state.sixdimData
      ? renderSixdimCards(state.sixdimData)
      : `<div class="sixdim-empty">
          <p class="muted" style="margin:0 0 12px;">六维内容由大模型基于当前舆情数据归纳生成，点击下方按钮开始。</p>
          <button type="button" class="btn btn--primary" id="btn-generate-sixdim">用大模型生成六维分析</button>
        </div>`;
  return `
  <div class="insight-cockpit">
    <div class="cockpit">
      <div class="cockpit__left">
        <div>
          <div class="cockpit__title">${title}</div>
          <div class="cockpit__meta">${meta}</div>
        </div>
        <span class="cockpit__badge badge--warning">AI 研判</span>
      </div>
      <div class="cockpit__actions">
        <button type="button" class="btn btn--secondary" id="btn-insight-export">${state.reportCache[state.sixdimScope || "all"] ? "查看研判报告" : "生成研判报告"}</button>
        <button type="button" class="btn btn--primary" id="btn-insight-plan"${
          state.sixdimLoading || (!state.sixdimData && !(state.planCache[state.sixdimScope || "all"] && state.planCache[state.sixdimScope || "all"].md))
            ? ' disabled title="请先生成六维分析"'
            : ""
        }>${state.planCache[state.sixdimScope || "all"] && state.planCache[state.sixdimScope || "all"].md ? "查看应对方案" : "生成应对方案"}</button>
      </div>
    </div>

    <div class="review-bar">
      <span class="review-bar__text">以下「情感迁移」「叙事框架」「深层影响」由 AI 归纳生成，涉及重大判断建议人工复核后使用。</span>
      <button type="button" class="review-bar__link" id="btn-insight-basis">查看推理依据 →</button>
    </div>

    ${renderMiniCharts(state.metricsData)}

    ${sixdimBody}${renderActions(state.sixdimData ? state.sixdimData.actions : null)}
  </div>`;
}

function renderEventResult() {
  const ev = events.find((e) => e.id === state.eventId) || events[0];
  return `
  <aside class="sider">
    <button type="button" class="sider__btn secondary" id="btn-event-list">← 返回列表</button>
    <div class="sider__title">任务</div>
    ${events
      .map((e) => {
        const locked = state.sixdimLoading && e.id !== ev.id;
        return `<button type="button" class="sider-item ${e.id === ev.id ? "is-active" : ""} ${locked ? "is-disabled" : ""}" data-open-event="${e.id}" ${locked ? 'aria-disabled="true" title="六维分析生成中，请稍候再切换"' : ""}>
        <div class="sider-item__name">${escapeHtml(e.name)}</div>
        <div class="sider-item__meta">${escapeHtml(e.status)}</div>
      </button>`;
      })
      .join("")}
  </aside>
  <main class="main">
    <div class="toolbar">
      <div class="crumbs">事件分析 / <strong>${escapeHtml(ev.name)}</strong> / 六维报告</div>
      <button type="button" class="btn-secondary" id="btn-event-list-top">返回列表</button>
    </div>
    ${renderSixLayerInsight(ev.name, "event")}
  </main>`;
}

function renderEventCreate() {
  const f = state.eventForm;
  const emptyChat = !state.eventChat.length;
  return `
  <main class="main main--plan">
    <div class="plan-banner">创建事件分析任务：填写任务名称、时间段与涉及词后即可生成分析。新手可先在右侧用自然语言描述，由助手代填表单；填完后仍可手改或继续对话修改。</div>
    <div class="plan-layout">
      <section class="panel plan-form-panel">
        <header class="panel__head event-form-head">
          <h3>创建事件分析任务</h3>
          <button type="button" class="btn-ghost" id="btn-event-list">返回列表</button>
        </header>
        <div class="plan-form" data-scroll-key="plan-form">
          <div class="plan-row">
            <label><i class="req">*</i>事件分析任务名称</label>
            <div class="plan-field">
              <input id="event-name" placeholder="请设置导出任务名称" value="${escapeHtml(f.name)}" />
            </div>
          </div>
          <div class="plan-row">
            <label><i class="req">*</i>任务时间段</label>
            <div class="plan-field plan-field--dates">
              <input id="event-start" placeholder="yyyy/mm/dd" value="${escapeHtml(f.start)}" />
              <span class="muted">至</span>
              <input id="event-end" placeholder="yyyy/mm/dd" value="${escapeHtml(f.end)}" />
              <span class="plan-hint">*时间范围最大一年，最小一天</span>
            </div>
          </div>
          <div class="plan-row plan-row--top">
            <label><i class="req">*</i>事件涉及词</label>
            <div class="plan-field">
              <textarea id="event-keywords" rows="6" placeholder="关键词之间请用以下“+”、“|”、“(”、“）”">${escapeHtml(f.keywords)}</textarea>
              <p class="plan-tip">+ 表示并且，| 表示或者；可用 ( ) 组合。</p>
            </div>
          </div>
          <div class="plan-row plan-row--top">
            <label>事件屏蔽词</label>
            <div class="plan-field">
              <textarea id="event-exclude" rows="5" placeholder="屏蔽词之间请用以下“+”、“|”、“(”、“）”">${escapeHtml(f.exclude)}</textarea>
            </div>
          </div>
          <div class="plan-actions">
            <button type="button" class="btn-primary" id="btn-event-save">确定</button>
            <button type="button" class="btn-secondary" id="btn-event-cancel">取消</button>
          </div>
        </div>
      </section>

      <section class="panel plan-agent-panel">
        <header class="panel__head">
          <h3>智能助手填表</h3>
          <span class="tag-mini">可改字段 / 可对话修改</span>
        </header>
        <div class="chat" id="event-agent-chat" data-scroll-key="event-chat">
          ${
            emptyChat
              ? `<div class="empty-hero">
                  <h2>描述你要深挖的事件</h2>
                  <p>助手会填写左侧表单。填完后你可以：① 直接改表单；② 继续说「屏蔽词加上股市」「时间改成近 7 天」等。</p>
                  <div class="examples">
                    <button type="button" class="chip" data-event-fill="分析近一个月美国关税对华科技与出口的舆情，排除股市行情">近一个月美国关税，排除股市</button>
                    <button type="button" class="chip" data-event-fill="梳理某品牌召回事件近 14 天传播路径，排除广告软文">召回事件近 14 天</button>
                    <button type="button" class="chip" data-event-fill="帮我建一个地方政府新政发布后的舆情事件分析">地方新政事件分析</button>
                  </div>
                </div>`
              : state.eventChat
                  .map((m) =>
                    m.role === "user"
                      ? `<div class="bubble bubble--user">${escapeHtml(m.text)}</div>`
                      : `<div class="bubble bubble--agent">${m.html}</div>`
                  )
                  .join("")
          }
        </div>
        <div class="composer">
          <input id="event-agent-input" placeholder="例如：涉及词改成 召回+(缺陷|投诉)，时间近 7 天" />
          <button type="button" class="btn-primary" id="btn-event-agent-send">发送</button>
        </div>
      </section>
    </div>
  </main>`;
}

function renderAssistant() {
  const empty = !state.assistMessages.length;
  return `
  <main class="main main--assist">
    <div class="assist-layout">
      <aside class="assist-side">
        <div class="brand-mini">智能助手</div>
        <div class="sider__title" style="opacity:1;color:var(--muted)">助手能力</div>
        <button type="button" class="assist-nav ${state.assistMode === "topic" ? "is-active" : ""}" data-assist-mode="topic">配置监测主题</button>
        <button type="button" class="assist-nav ${state.assistMode === "event" ? "is-active" : ""}" data-assist-mode="event">配置事件分析</button>
        <button type="button" class="assist-nav ${state.assistMode === "qa" ? "is-active" : ""}" data-assist-mode="qa">舆情问答 / 口径</button>
        <p class="muted assist-side__hint">用于降低复杂配置门槛；日常巡检请用工作台、主题监测、全文搜索。</p>
        <button type="button" class="btn-secondary" data-go="monitor">回到主题监测</button>
        <button type="button" class="btn-secondary" data-go="search">去全文搜索</button>
      </aside>
      <section class="assist-chat">
        <header class="panel__head">
          <h3>AGENT 对话</h3>
          <span class="tag-mini">${
            state.assistMode === "event" ? "事件分析" : state.assistMode === "qa" ? "问答研判" : "监测配置"
          }</span>
        </header>
        <div class="chat" id="assist-chat">
          ${
            empty
              ? `<div class="empty-hero">
                  <h2>${
                    state.assistMode === "event"
                      ? "描述你要深挖的事件"
                      : state.assistMode === "qa"
                        ? "直接提问即可"
                        : "用一句话说明你要盯什么"
                  }</h2>
                  <p>${
                    state.assistMode === "event"
                      ? "不用说布尔公式。说明事件、时间、包含/排除内容，我会生成任务字段供你确认。"
                      : state.assistMode === "qa"
                        ? "可追问负面点、传播路径或应对口径；回答会尽量带来源说明。"
                        : "不必填写关键词公式。说出品牌/事件、时间、关注正面还是负面即可。"
                  }</p>
                  <div class="examples">
                    <button type="button" class="chip" data-fill="${
                      state.assistMode === "event"
                        ? "分析近一个月美国关税对华科技与出口的舆情"
                        : state.assistMode === "qa"
                          ? "总结小米 SU7 近 24 小时负面点并给应对口径"
                          : "监控小米 SU7 近 7 天口碑，重点看负面"
                    }">${
                      state.assistMode === "event"
                        ? "分析近一个月美国关税对华科技与出口的舆情"
                        : state.assistMode === "qa"
                          ? "总结小米 SU7 近 24 小时负面点并给应对口径"
                          : "监控小米 SU7 近 7 天口碑，重点看负面"
                    }</button>
                    <button type="button" class="chip" data-fill="${
                      state.assistMode === "event"
                        ? "梳理某品牌召回事件近 14 天传播路径，排除广告软文"
                        : state.assistMode === "qa"
                          ? "现在风险高不高？要不要升级预警"
                          : "盯一下本周新能源降价相关舆情，只要微博和新闻"
                    }">${
                      state.assistMode === "event"
                        ? "梳理某品牌召回事件近 14 天传播路径，排除广告软文"
                        : state.assistMode === "qa"
                          ? "现在风险高不高？要不要升级预警"
                          : "盯一下本周新能源降价相关舆情，只要微博和新闻"
                    }</button>
                  </div>
                </div>`
              : state.assistMessages
                  .map((m) =>
                    m.role === "user"
                      ? `<div class="bubble bubble--user">${escapeHtml(m.text)}</div>`
                      : `<div class="bubble bubble--agent">${m.html}</div>`
                  )
                  .join("")
          }
        </div>
        <div class="composer">
          <input id="assist-input" placeholder="继续用自然语言说明需求…" />
          <button type="button" class="btn-primary" id="btn-assist-send">发送</button>
        </div>
      </section>
      <aside class="assist-insight">
        <header class="insight-head">
          <h3>助手面板</h3>
          <span class="status-pill">待命</span>
        </header>
        <p class="muted" style="margin:0;line-height:1.55;font-size:13px;">
          确认参数后，结果会写回「主题监测 / 事件分析」；此处展示当前助手上下文说明。
        </p>
        <ul class="assist-tips">
          <li>对话负责定规则与追问</li>
          <li>参数卡片需确认后才创建任务</li>
          <li>高级条件可回退原表单</li>
          <li>列表筛查请回主题监测 / 全文搜索</li>
        </ul>
      </aside>
    </div>
  </main>`;
}

// ===== 设置页：API 配置 =====
const SENTIMENT_PROVIDERS = [
  { value: "rule", label: "规则词典", desc: "免费·离线，内置正负面词表，作为演示与降级" },
  { value: "aliyun", label: "阿里云 NLP", desc: "专用情感 API，需 AccessKey ID / Secret" },
  { value: "xfyun", label: "讯飞开放平台", desc: "专用情感 API，需 APPID / APIKey" },
  { value: "selfhosted", label: "私有化 RoBERTa/BERT", desc: "自建服务 URL，政企推荐，最合规" },
];

async function loadConfig() {
  try {
    const r = await fetch("/api/config").then((r) => r.json());
    state.config = r.data || {};
  } catch (e) {
    state.config = state.config || {};
  }
  if (state.module === "settings") render();
}

async function loadSentimentResult() {
  try {
    const r = await fetch("/api/sentiment-result").then((r) => r.json());
    state.sentimentResult = r.data || null;
  } catch (e) {
    state.sentimentResult = null;
  }
  if (state.module === "settings") render();
}

async function saveConfig() {
  const provider = state.configProvider || (state.config && state.config.sentiment_provider) || "rule";
  const payload = {
    sentiment_provider: provider,
    aliyun_ak_id: $("#cfg-aliyun-id")?.value.trim() || "",
    aliyun_ak_secret: $("#cfg-aliyun-secret")?.value.trim() || "",
    xfyun_appid: $("#cfg-xfyun-appid")?.value.trim() || "",
    xfyun_apikey: $("#cfg-xfyun-apikey")?.value.trim() || "",
    selfhosted_url: $("#cfg-selfhosted-url")?.value.trim() || "",
    llm_base_url: $("#cfg-llm-base")?.value.trim() || "",
    llm_api_key: $("#cfg-llm-key")?.value.trim() || "",
    llm_model: $("#cfg-llm-model")?.value.trim() || "",
  };
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
    state.config = r.data || payload;
    state.configProvider = provider;
    toast("配置已保存，立即生效");
  } catch (e) {
    toast("保存失败：数据服务未运行");
  }
  render();
}

async function recomputeSentiment() {
  state.recomputing = true;
  render();
  try {
    const r = await fetch("/api/recompute-sentiment", { method: "POST" }).then((r) => r.json());
    state.sentimentResult = r.data || null;
    toast("已用当前引擎重新计算全部文章情感");
  } catch (e) {
    toast("重算失败：数据服务未运行");
  }
  state.recomputing = false;
  render();
}

function providerFields(provider) {
  const cfg = state.config || {};
  if (provider === "aliyun") {
    return `
      <div class="cfg-field"><label>AccessKey ID</label>
        <input id="cfg-aliyun-id" value="${escapeHtml(cfg.aliyun_ak_id || "")}" placeholder="LTAI..." /></div>
      <div class="cfg-field"><label>AccessKey Secret</label>
        <input id="cfg-aliyun-secret" type="password" placeholder="${cfg.aliyun_ak_secret && cfg.aliyun_ak_secret.configured ? "已配置（留空保持不变）" : "..."}" /></div>`;
  }
  if (provider === "xfyun") {
    return `
      <div class="cfg-field"><label>APPID</label>
        <input id="cfg-xfyun-appid" value="${escapeHtml(cfg.xfyun_appid || "")}" placeholder="..." /></div>
      <div class="cfg-field"><label>APIKey</label>
        <input id="cfg-xfyun-apikey" type="password" placeholder="${cfg.xfyun_apikey && cfg.xfyun_apikey.configured ? "已配置（留空保持不变）" : "..."}" /></div>`;
  }
  if (provider === "selfhosted") {
    return `
      <div class="cfg-field"><label>服务 URL</label>
        <input id="cfg-selfhosted-url" value="${escapeHtml(cfg.selfhosted_url || "")}" placeholder="http://127.0.0.1:8001/sentiment" /></div>`;
  }
  return `<div class="cfg-note">使用内置正负面词典，无需任何配置。未配置云端密钥时也会自动降级到此引擎，保证管线不中断。</div>`;
}

function resultHtml(res) {
  if (!res || !res.emotion) {
    return `<div class="empty-block">尚未计算。点击右上角「重新计算全部文章」即可看到引擎打分结果。</div>`;
  }
  const pos = res.emotion["正面"] || 0;
  const neu = res.emotion["中性"] || 0;
  const neg = res.emotion["负面"] || 0;
  const total = Math.max(1, pos + neu + neg);
  const pPct = Math.round((pos / total) * 100);
  const nPct = Math.round((neu / total) * 100);
  const gPct = 100 - pPct - nPct;
  const allItems = res.items || [];
  const SHOW_COUNT = 30;
  const showAll = !!state.showAllArticles;
  const visible = showAll ? allItems : allItems.slice(0, SHOW_COUNT);
  const rows = visible.map((a) => {
    const cls = a.sentiment_label === "负面" ? "neg" : a.sentiment_label === "正面" ? "pos" : "";
    return `
      <div class="res-row">
        <span class="res-tag ${cls}">${escapeHtml(a.sentiment_label || "")}</span>
        <div class="res-body">
          <div class="res-title">${escapeHtml(a.title || "（无标题）")}</div>
          <div class="res-meta">${escapeHtml(a.source || "")} · 置信度 ${a.confidence != null ? Math.round(a.confidence * 100) + "%" : "—"}</div>
        </div>
      </div>`;
  }).join("");
  const moreBtn = allItems.length > SHOW_COUNT
    ? `<button type="button" class="btn-secondary res-more" data-toggle-articles>${showAll ? "收起" : `展开全部 ${allItems.length} 条`}</button>`
    : "";
  return `
    <div class="result-overview">
      <div class="result-stats">
        <div class="stat"><div class="stat__num">${res.total}</div><div class="stat__label">总数</div></div>
        <div class="stat pos"><div class="stat__num">${pos}</div><div class="stat__label">正面</div></div>
        <div class="stat neu"><div class="stat__num">${neu}</div><div class="stat__label">中性</div></div>
        <div class="stat neg"><div class="stat__num">${neg}</div><div class="stat__label">负面</div></div>
      </div>
      <div class="result-bar">
        <span class="bar-pos" style="width:${pPct}%"></span><span class="bar-neu" style="width:${nPct}%"></span><span class="bar-neg" style="width:${gPct}%"></span>
      </div>
      <div class="result-bar-legend">
        <span><i class="dot--pos"></i>正面 ${pPct}%</span>
        <span><i class="dot--neu"></i>中性 ${nPct}%</span>
        <span><i class="dot--neg"></i>负面 ${gPct}%</span>
      </div>
      <div class="muted" style="margin-top:6px;">当前引擎：${escapeHtml(res.provider || "rule")}</div>
    </div>
    <div class="result-list">
      <div class="result-list__head">文章明细（负面优先，默认显示 ${Math.min(SHOW_COUNT, allItems.length)} / ${allItems.length} 条）</div>
      ${rows || '<div class="empty-block">暂无文章</div>'}
      ${moreBtn}
    </div>`;
}

async function loadInsightEmotion() {
  state.insightLoading = true;
  state.insightResult = null;
  render();
  toast("正在调用大模型归纳…");
  try {
    const r = await fetch("/api/insight-emotion", { method: "POST" }).then((r) => r.json());
    state.insightResult = r.data || null;
  } catch (e) {
    state.insightResult = { available: false, error: "请求失败：数据服务未运行" };
  }
  state.insightLoading = false;
  render();
  const ok = !!(state.insightResult && state.insightResult.emotion);
  if (ok) {
    toast("归纳完成");
  } else {
    const err = (state.insightResult && state.insightResult.error) || "未知原因";
    toast("归纳失败：" + err);
  }
}

function insightHtml(res, loading) {
  if (loading) {
    return `<div class="insight-loading">正在调用大模型归纳，请稍候…（大模型推理约需数秒到数十秒）</div>`;
  }
  if (!res) {
    return `<div class="empty-block">点击上方「生成归纳」按钮，用大模型对当前全部文章做「情感与态度」归纳（态度强度 / 立场分化 / 情感迁移 / 理由）。</div>`;
  }
  if (res.available === false || !res.emotion) {
    return `<div class="insight-error">
      <div class="insight-error__title">归纳失败</div>
      <div class="insight-error__msg">${escapeHtml(res.error || "未知错误")}</div>
      <div class="insight-error__hint">常见原因：① 大模型 API Key 无效或额度不足；② 网络超时；③ 模型返回格式异常。可点击「生成归纳」重试。</div>
    </div>`;
  }
  const emo = res.emotion;
  const strength = emo.attitude_strength || {};
  const stance = emo.stance_split || {};
  const reasons = emo.reasons || [];
  const strengthBars = Object.entries(strength).map(([k, v]) => `
    <div class="meter-row">
      <span class="meter-label">${escapeHtml(k)}</span>
      <div class="meter-track"><div class="meter-fill meter-fill--strength" style="width:${v}%"></div></div>
      <span class="meter-val">${v}%</span>
    </div>`).join("");
  const stanceBars = Object.entries(stance).map(([k, v]) => `
    <div class="meter-row">
      <span class="meter-label">${escapeHtml(k)}</span>
      <div class="meter-track"><div class="meter-fill meter-fill--stance" style="width:${v}%"></div></div>
      <span class="meter-val">${v}%</span>
    </div>`).join("");
  return `
    <div class="insight-overview">
      <div class="insight-badges">
        <div class="insight-badge"><span>主导情绪</span><strong>${escapeHtml(emo.dominant_emotion || "无")}</strong></div>
        <div class="insight-badge"><span>归纳置信度</span><strong>${emo.confidence != null ? Math.round(emo.confidence * 100) + "%" : "—"}</strong></div>
      </div>
      <div class="insight-section">
        <div class="insight-section__title">情感迁移</div>
        <div class="insight-shift">${escapeHtml(emo.emotion_shift || "暂无足够数据")}</div>
      </div>
      <div class="insight-section">
        <div class="insight-section__title">态度强度分布</div>
        ${strengthBars || '<div class="muted">暂无数据</div>'}
      </div>
      <div class="insight-section">
        <div class="insight-section__title">立场分化</div>
        ${stanceBars || '<div class="muted">暂无数据</div>'}
      </div>
      ${reasons.length ? `
      <div class="insight-section">
        <div class="insight-section__title">关键归因</div>
        <ul class="insight-reasons">${reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>
      </div>` : ""}
      <div class="insight-review">⚠ 本块为 AI 归纳，仅供复核参考（对齐 PRD 人工复核要求）</div>
    </div>`;
}

function renderSettings() {
  const cfg = state.config || {};
  const provider = state.configProvider || cfg.sentiment_provider || "rule";
  return `
  <main class="main main--settings" data-scroll-key="settings-main">
    <div class="hero-line">
      <div>
        <h1>设置</h1>
        <p class="muted">配置情感分析引擎与大模型，让 ${state.sentimentResult ? state.sentimentResult.total : "全部"} 篇文章的情感由引擎真实计算（改动仅在本页，不影响其他模块）。</p>
      </div>
    </div>

    <div class="settings-grid">
      <section class="panel settings-card">
        <header class="panel__head"><h3>情感分析引擎</h3><span class="muted">入库批量打标 · 正/中/负</span></header>
        <div class="panel__body">
          <div class="provider-list">
            ${SENTIMENT_PROVIDERS.map((p) => `
              <button type="button" class="provider-item ${provider === p.value ? "is-active" : ""}" data-provider="${p.value}">
                <div class="provider-item__name">${p.label}</div>
                <div class="provider-item__desc">${p.desc}</div>
              </button>`).join("")}
          </div>
          <div class="cfg-fields">${providerFields(provider)}</div>
        </div>
      </section>

      <section class="panel settings-card">
        <header class="panel__head"><h3>大模型（可选）</h3><span class="muted">态度/立场/情感迁移/理由</span></header>
        <div class="panel__body">
          <div class="cfg-field"><label>Base URL</label>
            <input id="cfg-llm-base" value="${escapeHtml(cfg.llm_base_url || "")}" placeholder="https://api.openai.com/v1" /></div>
          <div class="cfg-field"><label>API Key</label>
            <input id="cfg-llm-key" type="password" placeholder="${cfg.llm_api_key && cfg.llm_api_key.configured ? "已配置（留空保持不变）" : "sk-..."}" /></div>
          <div class="cfg-field"><label>模型</label>
            <input id="cfg-llm-model" value="${escapeHtml(cfg.llm_model || "")}" placeholder="gpt-4o-mini" /></div>
          <div class="cfg-note">仅用于智能助手的 AI 情感归纳；不配置也能用规则问答，不会联网、不会编造。</div>
        </div>
      </section>
    </div>

    <div class="settings-actions">
      <button type="button" class="btn-primary" id="btn-save-config">保存配置</button>
    </div>
  </main>`;
}

function openModal(type) {
  const wrap = document.createElement("div");
  wrap.className = "modal-mask";
  wrap.id = "modal";
  if (type === "new-topic") {
    wrap.innerHTML = `
      <div class="modal">
        <h3>新建监测主题</h3>
        <p>可以选择经典表单，或交给智能助手用对话生成监测条件（推荐新手）。</p>
        <div class="modal__actions">
          <button type="button" class="btn-ghost" data-close-modal>取消</button>
          <button type="button" class="btn-secondary" data-classic-topic>经典表单</button>
          <button type="button" class="btn-primary" data-go-assist-topic>智能助手配置</button>
        </div>
      </div>`;
  } else if (type === "event-form") {
    wrap.innerHTML = `
      <div class="modal">
        <h3>创建事件分析任务（经典表单）</h3>
        <p>字段：任务名称、时间段、涉及词、屏蔽词。演示环境不提交真实任务。</p>
        <div class="modal__actions">
          <button type="button" class="btn-ghost" data-close-modal>关闭</button>
          <button type="button" class="btn-primary" data-close-modal>确定</button>
        </div>
      </div>`;
  } else if (type && typeof type === "object" && type.title) {
    // 真实文章详情：展示摘要 + 原文链接
    const a = type;
    wrap.innerHTML = `
      <div class="modal">
        <h3>${escapeHtml(a.title)}</h3>
        <div style="color:#6b7a8d;font-size:13px;margin:6px 0 10px;">${escapeHtml(a.source)} · ${escapeHtml(a.time)} · <span style="color:${
          a.sentiment === "neg" ? "#a33b3b" : a.sentiment === "pos" ? "#3d8f6a" : "#c9a227"
        }">${sentLabel(a.sentiment)}</span></div>
        <p style="line-height:1.7;font-size:14px;">${escapeHtml(a.summary || "（暂无摘要）")}</p>
        <p>
          <a href="${a.url}" target="_blank" rel="noopener"
             style="display:inline-block;background:#1f6b5c;color:#fff;text-decoration:none;padding:9px 18px;border-radius:6px;font-size:14px;">
            查看原文 ↗
          </a>
        </p>
        <div class="modal__actions">
          <button type="button" class="btn-primary" data-close-modal>关闭</button>
        </div>
      </div>`;
  }
  document.body.appendChild(wrap);
  wrap.addEventListener("click", (e) => {
    if (e.target === wrap || e.target.matches("[data-close-modal]")) {
      state.modal = null;
      wrap.remove();
    }
    if (e.target.matches("[data-go-assist-topic]")) {
      state.modal = null;
      wrap.remove();
      openPlanCreate();
    }
    if (e.target.matches("[data-classic-topic]")) {
      state.modal = null;
      wrap.remove();
      openPlanCreate();
      toast("已打开监测方案创建表单");
    }
  });
}

function bind() {
  document.querySelectorAll("[data-go]").forEach((el) => {
    el.addEventListener("click", () => {
      if (state.sixdimLoading && el.dataset.topic && el.dataset.topic !== state.topicId) {
        toast("六维分析生成中，请稍候再切换主题");
        return;
      }
      if (el.dataset.topic) state.topicId = el.dataset.topic;
      setModule(el.dataset.go);
    });
  });

  document.querySelectorAll("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.monitorView = btn.dataset.view;
      if (btn.dataset.view === "insight") {
        state.sixdimScope = "topic:" + state.topicId;
        state.sixdimData = state.sixdimCache[state.sixdimScope] || null;
        loadMetrics();
      }
      render();
    });
  });

  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filters[btn.dataset.filter] = btn.dataset.value;
      render();
      toast("已更新筛选");
    });
  });

  document.querySelectorAll("[data-article]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const art = articles.find((a) => a.id === Number(btn.dataset.article));
      if (!art) return toast("文章详情不可用");
      state.modal = art;
      openModal(state.modal);
    });
  });

  document.querySelectorAll("[data-hot], [data-history]").forEach((el) => {
    el.addEventListener("click", () => {
      const q = el.dataset.hot || el.dataset.history;
      state.searchQ = q;
      state.searchDone = true;
      setModule("search");
      toast(`已搜索：${q}`);
    });
  });

  const btnSearch = $("#btn-search");
  if (btnSearch) {
    btnSearch.onclick = () => {
      state.searchQ = $("#search-input")?.value.trim() || "";
      state.searchDone = true;
      render();
      toast(state.searchQ ? "已返回搜索结果" : "请输入关键词");
    };
  }
  const searchInput = $("#search-input");
  if (searchInput) {
    searchInput.onkeydown = (e) => {
      if (e.key === "Enter") btnSearch?.click();
    };
  }

  const btnNewTopic = $("#btn-new-topic");
  if (btnNewTopic) {
    btnNewTopic.onclick = () => openPlanCreate();
  }
  const btnAssistTopic = $("#btn-assist-topic");
  if (btnAssistTopic) {
    btnAssistTopic.onclick = () => openPlanCreate();
  }

  // plan create form + agent
  const planSend = $("#btn-plan-agent-send");
  if (planSend) {
    planSend.onclick = sendPlanAgent;
    $("#plan-agent-input").onkeydown = (e) => {
      if (e.key === "Enter") sendPlanAgent();
    };
  }
  document.querySelectorAll("[data-plan-fill]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const input = $("#plan-agent-input");
      if (input) {
        input.value = chip.dataset.planFill;
        input.focus();
        toast("已填入示例，点击发送");
      }
    });
  });
  const planSave = $("#btn-plan-save");
  if (planSave) {
    planSave.onclick = async () => {
      syncPlanFormFromDom();
      const f = state.planForm;
      if (!f.name) return toast("请填写方案名称");
      if (!f.keywords) return toast("请填写主体关键词");
      try {
        await fetch("/api/themes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: f.name, group: f.group, keywords: f.keywords,
            exclude: f.exclude, alert: f.alert,
          }),
        });
        await loadData(); // 重新拉取主题与文章（用后端分配的主题 id）
        state.monitorView = "list";
        const nt = topics.find((t) => t.name === f.name);
        if (nt) state.topicId = nt.id;
        toast("监测方案已保存");
      } catch (e) {
        toast("保存失败：数据服务未运行");
      }
      render();
    };
  }
  const planCancel = $("#btn-plan-cancel");
  if (planCancel) {
    planCancel.onclick = () => {
      state.monitorView = "list";
      render();
    };
  }
  const planAlert = $("#plan-alert");
  if (planAlert) {
    planAlert.onchange = () => {
      syncPlanFormFromDom();
      if (state.planForm.alert) {
        state.planForm.alertName = state.planForm.alertName || state.planForm.name || "风险预警";
        state.planForm.alertWords =
          state.planForm.alertWords ||
          state.planForm.keywords.replace(/[+|()]/g, ",").replace(/,+/g, ",").replace(/^,|,$/g, "") ||
          "";
      }
      render();
    };
  }
  document.querySelectorAll(".alert-box .opt").forEach((btn) => {
    btn.addEventListener("click", () => {
      syncPlanFormFromDom();
      const multi = btn.hasAttribute("data-alert-source");
      if (multi) {
        const val = btn.dataset.alertSource;
        let cur = [...state.planForm.alertSources];
        if (val === "全部") {
          cur = ["全部"];
        } else {
          cur = cur.filter((x) => x !== "全部");
          if (cur.includes(val)) cur = cur.filter((x) => x !== val);
          else cur.push(val);
          if (!cur.length) cur = ["全部"];
        }
        state.planForm.alertSources = cur;
        const wrap = btn.closest(".opt-wrap");
        wrap.querySelectorAll(".opt").forEach((o) => {
          o.classList.toggle("is-active", cur.includes(o.dataset.alertSource));
        });
      } else {
        const map = {
          alertContent: "alertContent",
          alertMatch: "alertMatch",
          alertChannel: "alertChannel",
          alertWeekend: "alertWeekend",
          alertMerge: "alertMerge",
          alertDedup: "alertDedup",
          alertInterval: "alertInterval",
        };
        for (const [domKey, stateKey] of Object.entries(map)) {
          if (btn.dataset[domKey] !== undefined) {
            state.planForm[stateKey] = btn.dataset[domKey];
            const wrap = btn.closest(".opt-wrap");
            wrap.querySelectorAll(".opt").forEach((o) => o.classList.remove("is-active"));
            btn.classList.add("is-active");
            if (domKey === "alertInterval") {
              const sel = $("#alert-interval-hours");
              if (sel) sel.disabled = state.planForm.alertInterval !== "定时预警";
            }
          }
        }
      }
    });
  });

  // data-jump-list / 侧栏主题切换：见下方 #shell 事件委托
  document.querySelectorAll("[data-edit-topic]").forEach((btn) => {
    btn.addEventListener("click", () => toast(`演示：编辑主题「${btn.dataset.editTopic}」`));
  });

  const btnNewEvent = $("#btn-new-event");
  if (btnNewEvent) btnNewEvent.onclick = () => openEventCreate();
  const btnEventList = $("#btn-event-list");
  if (btnEventList) {
    btnEventList.onclick = () => {
      state.eventMode = "list";
      render();
    };
  }
  document.querySelectorAll("[data-open-event]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.sixdimLoading && btn.dataset.openEvent !== state.eventId) {
        toast("六维分析生成中，请稍候再切换任务");
        return;
      }
      state.eventId = btn.dataset.openEvent;
      state.eventMode = "result";
      state.sixdimScope = "event:" + btn.dataset.openEvent;
      state.sixdimData = state.sixdimCache[state.sixdimScope] || null;
      loadMetrics();
      render();
    });
  });
  const btnEventListTop = $("#btn-event-list-top");
  if (btnEventListTop) {
    btnEventListTop.onclick = () => {
      state.eventMode = "list";
      render();
    };
  }
  // 六维报告操作按钮
  const btnInsightExport = $("#btn-insight-export");
  if (btnInsightExport) btnInsightExport.onclick = viewReport;
  const btnInsightPlan = $("#btn-insight-plan");
  if (btnInsightPlan) {
    btnInsightPlan.onclick = viewPlan;
  }
  const btnInsightScript = $("#btn-insight-script");
  if (btnInsightScript) btnInsightScript.onclick = () => toast("演示：已按 PRD 模板生成回应话术");
  const btnInsightBasis = $("#btn-insight-basis");
  if (btnInsightBasis) btnInsightBasis.onclick = () => toast("演示：展示模型引用的原文与规则依据");
  const btnGenerateSixdim = $("#btn-generate-sixdim");
  if (btnGenerateSixdim) btnGenerateSixdim.onclick = generateSixdim;
  document.querySelectorAll("[data-insight-toast]").forEach((el) => {
    el.addEventListener("click", () => toast(el.dataset.insightToast));
  });

  const eventAgentSend = $("#btn-event-agent-send");
  if (eventAgentSend) {
    eventAgentSend.onclick = sendEventAgent;
    $("#event-agent-input").onkeydown = (e) => {
      if (e.key === "Enter") sendEventAgent();
    };
  }
  document.querySelectorAll("[data-event-fill]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const input = $("#event-agent-input");
      if (input) {
        input.value = chip.dataset.eventFill;
        input.focus();
        toast("已填入示例，点击发送");
      }
    });
  });
  const eventSave = $("#btn-event-save");
  if (eventSave) {
    eventSave.onclick = () => {
      syncEventFormFromDom();
      const f = state.eventForm;
      if (!f.name) return toast("请填写任务名称");
      if (!f.start || !f.end) return toast("请填写时间段");
      if (!f.keywords) return toast("请填写事件涉及词");
      events.unshift({
        id: "e" + Date.now(),
        name: f.name,
        createdAt: "2026-08-12",
        status: "已生成",
      });
      state.eventMode = "list";
      toast("事件分析任务已创建");
      render();
    };
  }
  const eventCancel = $("#btn-event-cancel");
  if (eventCancel) {
    eventCancel.onclick = () => {
      state.eventMode = "list";
      render();
    };
  }

  document.querySelectorAll("[data-fill]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const input = $("#assist-input") || $("#event-input");
      if (input) {
        input.value = chip.dataset.fill;
        input.focus();
        toast("已填入示例");
      }
    });
  });

  document.querySelectorAll("[data-assist-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.assistMode;
      if (mode === "topic") {
        openPlanCreate();
        return;
      }
      if (mode === "event") {
        openEventCreate();
        return;
      }
      state.assistMode = mode;
      state.assistMessages = [];
      render();
    });
  });

  const assistSend = $("#btn-assist-send");
  if (assistSend) {
    assistSend.onclick = sendAssist;
    $("#assist-input").onkeydown = (e) => {
      if (e.key === "Enter") sendAssist();
    };
  }

  document.querySelectorAll("[data-confirm-assist]").forEach((btn) => {
    btn.addEventListener("click", () => {
      toast("已写入主题监测（演示）");
      setModule("monitor");
    });
  });

  // 设置页：API 配置 + 情感重算
  document.querySelectorAll("[data-provider]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.configProvider = btn.dataset.provider;
      render();
    });
  });
  const saveCfg = $("#btn-save-config");
  if (saveCfg) saveCfg.onclick = saveConfig;
  const refreshBtn = $("#btn-refresh-data");
  if (refreshBtn) refreshBtn.onclick = refreshData;
  const recomputeBtn = $("#btn-recompute");
  if (recomputeBtn) recomputeBtn.onclick = recomputeSentiment;
  const insightBtn = $("#btn-insight");
  if (insightBtn) insightBtn.onclick = loadInsightEmotion;
  document.querySelectorAll("[data-toggle-articles]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.showAllArticles = !state.showAllArticles;
      render();
    });
  });
}

function sendPlanAgent() {
  syncPlanFormFromDom();
  const input = $("#plan-agent-input");
  const text = (input?.value || "").trim();
  if (!text) return toast("请先输入需求");
  input.value = "";
  const isFirst = state.planChat.length === 0;
  state.planChat.push({ role: "user", text });
  applyAgentFill(text, !isFirst && !!(state.planForm.keywords || state.planForm.name));
  const f = state.planForm;
  state.planChat.push({
    role: "agent",
    html: `<p>已更新左侧表单，请核对：</p>
      <div class="param-card">
        <div class="param-card__row"><span>方案名称</span><strong>${escapeHtml(f.name || "（未填）")}</strong></div>
        <div class="param-card__row"><span>方案组</span><strong>${escapeHtml(f.group)}</strong></div>
        <div class="param-card__row"><span>主体关键词</span><strong>${escapeHtml(f.keywords || "（未填）")}</strong></div>
        <div class="param-card__row"><span>屏蔽歧义词</span><strong>${escapeHtml(f.exclude || "（无）")}</strong></div>
        <div class="param-card__row"><span>预警</span><strong>${f.alert ? "ON" : "OFF"}</strong></div>
        ${
          f.alert
            ? `<div class="param-card__row"><span>预警名称</span><strong>${escapeHtml(f.alertName || "（未填）")}</strong></div>
        <div class="param-card__row"><span>预警词</span><strong>${escapeHtml(f.alertWords || "（未填）")}</strong></div>
        <div class="param-card__row"><span>来源</span><strong>${escapeHtml((f.alertSources || []).join("、"))}</strong></div>`
            : ""
        }
      </div>
      <p style="margin:8px 0 0;font-size:13px;color:#6b7a8d;">可直接改左侧字段，或继续说「打开预警」「预警词改成异响,投诉」「只要微博和新闻」。</p>`,
  });
  render();
  toast("表单已由助手更新");
}

function sendEventAgent() {
  syncEventFormFromDom();
  const input = $("#event-agent-input");
  const text = (input?.value || "").trim();
  if (!text) return toast("请先输入需求");
  input.value = "";
  const isFirst = state.eventChat.length === 0;
  state.eventChat.push({ role: "user", text });
  applyEventFill(text, !isFirst && !!(state.eventForm.keywords || state.eventForm.name));
  const f = state.eventForm;
  state.eventChat.push({
    role: "agent",
    html: `<p>已更新左侧表单，请核对：</p>
      <div class="param-card">
        <div class="param-card__row"><span>任务名称</span><strong>${escapeHtml(f.name || "（未填）")}</strong></div>
        <div class="param-card__row"><span>时间段</span><strong>${escapeHtml(f.start)} — ${escapeHtml(f.end)}</strong></div>
        <div class="param-card__row"><span>涉及词</span><strong>${escapeHtml(f.keywords || "（未填）")}</strong></div>
        <div class="param-card__row"><span>屏蔽词</span><strong>${escapeHtml(f.exclude || "（无）")}</strong></div>
      </div>
      <p style="margin:8px 0 0;font-size:13px;color:#6b7a8d;">可直接改左侧字段，或继续说「屏蔽词加上××」「时间改成近 7 天」。</p>`,
  });
  render();
  toast("表单已由助手更新");
}

async function sendAssist() {
  const input = $("#assist-input");
  const text = (input?.value || "").trim();
  if (!text) return toast("请先输入内容");
  input.value = "";
  state.assistMessages.push({ role: "user", text });
  render();
  try {
    const r = await fetch("/api/assistant?q=" + encodeURIComponent(text)).then((r) => r.json());
    state.assistMessages.push({ role: "agent", html: r.data.html });
  } catch (e) {
    state.assistMessages.push({ role: "agent", html: "<p>分析服务暂不可用，请稍后再试。</p>" });
  }
  render();
}

// topnav
$("#topnav").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-module]");
  if (!btn) return;
  setModule(btn.dataset.module);
});

// 主题切换用事件委托（#shell 节点不销毁，避免每次 render 重绑失败/缓存旧逻辑）
$("#shell").addEventListener("click", (e) => {
  const jump = e.target.closest("[data-jump-list]");
  if (jump && jump.dataset.topic) {
    switchMonitorTopic(jump.dataset.topic, true);
    return;
  }
  const topicEl = e.target.closest("[data-topic]");
  if (!topicEl || !topicEl.dataset.topic) return;
  if (topicEl.dataset.jumpList !== undefined) return;
  // 工作台「需关注」→ 数据列表；左侧「我的主题」→ 保留当前子视图
  if (topicEl.closest(".attn-list")) {
    switchMonitorTopic(topicEl.dataset.topic, true);
  } else if (topicEl.closest(".sider")) {
    switchMonitorTopic(topicEl.dataset.topic, false);
  }
});

render();
loadData();


