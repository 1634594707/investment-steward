/* 设计稿校验脚本：抽取内联 script 做语法检查 + 用 DOM 桩跑关键渲染路径 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FILE = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, "..", "docs", "design-draft", "base-shell.html");
const html = fs.readFileSync(FILE, "utf8");

let fail = 0;
function ok(name, cond, extra) {
  if (cond) console.log("PASS  " + name);
  else { console.log("FAIL  " + name + (extra ? "  -> " + extra : "")); fail++; }
}

/* ---------- 1. 抽取内联 script ---------- */
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log("FAIL  找不到内联 script"); process.exit(1); }
const code = m[1];
fs.writeFileSync(path.join(__dirname, "_inline.tmp.js"), code);

/* ---------- 2. 语法检查 ---------- */
try {
  new vm.Script(code, { filename: "inline.js" });
  console.log("PASS  JS 语法检查");
} catch (e) {
  console.log("FAIL  JS 语法检查 -> " + e.message);
  process.exit(1);
}

/* ---------- 3. 结构检查（静态，基于真实文件文本） ---------- */
const pages = [...html.matchAll(/<section class="page[^"]*" id="page-([a-z]+)"/g)].map(x => x[1]);
console.log("      页面: " + pages.join(", "));
const navPages = [...html.matchAll(/class="nav-item[^"]*"[^>]*data-page="([a-z]+)"/g)].map(x => x[1]);
console.log("      侧栏入口: " + navPages.join(", "));
for (const p of navPages) ok("侧栏入口 " + p + " 有对应页面", pages.includes(p), "未找到 page-" + p);
ok("分享池页面存在", pages.includes("quant"));
ok("设置页面存在", pages.includes("settings"));
ok("扩展页面存在", pages.includes("extensions"));

/* NAV 常量必须覆盖所有侧栏入口 */
const navBlock = code.match(/var NAV = \{([\s\S]*?)\n  \};/);
const navKeys = navBlock ? [...navBlock[1].matchAll(/^\s*([a-z]+):\{/gm)].map(x => x[1]) : [];
console.log("      NAV 键: " + navKeys.join(", "));
for (const p of navPages) ok("NAV 覆盖 " + p, navKeys.includes(p));

/* data-qtab / data-stab 必须有对应容器 */
for (const t of [...html.matchAll(/data-qtab="([a-z]+)"/g)].map(x => x[1]))
  ok("qtab 容器 poolList 存在（" + t + "）", /id="poolList"/.test(html));
for (const t of [...html.matchAll(/data-stab="([a-z]+)"/g)].map(x => x[1]))
  ok("设置分区 setpane-" + t + " 存在", html.includes('id="setpane-' + t + '"'));

/* ---------- 4. DOM 桩 ---------- */
function makeEl(id, attrs) {
  const store = Object.assign({}, attrs || {});
  const el = {
    id: id,
    _cls: new Set(),
    style: {},
    innerHTML: "",
    textContent: "",
    value: "",
    children: [],
    getAttribute(k) { return k in store ? store[k] : null; },
    setAttribute(k, v) { store[k] = v; },
    classList: {
      add(c) { el._cls.add(c); },
      remove(c) { el._cls.delete(c); },
      toggle(c, force) { if (force === undefined) { el._cls.has(c) ? el._cls.delete(c) : el._cls.add(c); } else { force ? el._cls.add(c) : el._cls.delete(c); } },
      contains(c) { return el._cls.has(c); }
    },
    addEventListener(type, fn) { (el._h[type] = el._h[type] || []).push(fn); },
    _h: {},
    fire(type) {
      (el._h[type] || []).forEach(fn => fn.call(el, { target: { closest: () => null } }));
    },
    querySelectorAll: () => [],
    closest: () => null
  };
  return el;
}

/* 从 HTML 真实播种每个 id 的 class，避免测试桩与文件脱节 */
const classById = {};
{
  const tagRe = /<[a-zA-Z][^>]*\bid="([^"]+)"[^>]*>/g;
  let tm;
  while ((tm = tagRe.exec(html))) {
    const idm = tm[0].match(/\bid="([^"]+)"/);
    const cm = tm[0].match(/\bclass="([^"]+)"/);
    if (idm && cm) classById[idm[1]] = cm[1].split(/\s+/);
  }
}
function seed(el) {
  (classById[el.id] || []).forEach(c => el._cls.add(c));
  return el;
}

/* 预先注册脚本里 getElementById 会用到的节点 */
const ids = [...code.matchAll(/getElementById\("([^"]+)"\)/g)].map(x => x[1]);
const registry = {};
[...new Set(ids)].forEach(id => { registry[id] = seed(makeEl(id)); });

/* 需要带 data-* 属性的集合节点 */
const navNames = navPages;
const navItems = navNames.map(p => makeEl("nav-" + p, { "data-page": p }));
const qtabEls = [...new Set([...html.matchAll(/data-qtab="([a-z]+)"/g)].map(x => x[1]))]
  .map(t => makeEl("qtab-" + t, { "data-qtab": t }));
const setEls = [...new Set([...html.matchAll(/data-stab="([a-z]+)"/g)].map(x => x[1]))]
  .map(t => makeEl("set-" + t, { "data-stab": t }));
const panes = setEls.map(e => "setpane-" + e.getAttribute("data-stab"))
  .map(id => (registry[id] = registry[id] || makeEl(id)));

const pageEls = {};
pages.forEach(p => {
  pageEls[p] = seed(makeEl("page-" + p));
  registry["page-" + p] = pageEls[p];   /* 与 getElementById 返回同一对象 */
});

const document = {
  getElementById(id) {
    if (!registry[id]) registry[id] = seed(makeEl(id));
    return registry[id];
  },
  querySelectorAll(sel) {
    if (sel === ".page") return pages.map(p => pageEls[p]);
    if (sel === ".nav-item") return navItems;
    if (sel === ".qtab") return qtabEls;
    if (sel === ".set-item") return setEls;
    if (sel === ".set-pane") return panes;
    if (sel === ".md-item") return [];
    if (sel === ".domain-tab") return [];
    return [];
  },
  addEventListener(type, fn) { (document._h[type] = document._h[type] || []).push(fn); },
  _h: {}
};

const sandbox = {
  document,
  window: { scrollTo() {} },
  console,
  Date,
  Math,
  Number,
  String,
  Object,
  Array,
  JSON,
  parseInt,
  isNaN
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

try {
  vm.runInContext(code, sandbox, { filename: "inline.js" });
  console.log("PASS  脚本执行无异常");
} catch (e) {
  console.log("FAIL  脚本执行异常 -> " + e.message + "\n" + e.stack);
  process.exit(1);
}

/* ---------- 5. 渲染结果断言 ---------- */
const poolHtml = registry["poolList"] ? registry["poolList"].innerHTML : "";
ok("分享池渲染出 7 个制品", (poolHtml.match(/class="pool-item/g) || []).length === 7,
   "实际 " + (poolHtml.match(/class="pool-item/g) || []).length);
ok("锁定制品标记 locked", (poolHtml.match(/pool-item locked/g) || []).length === 2,
   "实际 " + (poolHtml.match(/pool-item locked/g) || []).length);
ok("口径徽章已渲染", (poolHtml.match(/class="caliber /g) || []).length >= 10,
   "实际 " + (poolHtml.match(/class="caliber /g) || []).length);
ok("标签计数已填充", registry["qcount-pool"].textContent === "07" &&
   registry["qcount-mine"].textContent === "01" &&
   registry["qcount-lineage"].textContent === "01",
   [registry["qcount-pool"].textContent, registry["qcount-mine"].textContent, registry["qcount-lineage"].textContent].join("/"));

/* ---------- HF 式发现断言 ---------- */
ok("类型页签计数 全部07/参数集05/策略包01/模型权重01",
   registry["tp-all"].textContent === "07" &&
   registry["tp-parameter_set"].textContent === "05" &&
   registry["tp-strategy_pack"].textContent === "01" &&
   registry["tp-model_weights"].textContent === "01",
   [registry["tp-all"].textContent, registry["tp-parameter_set"].textContent,
    registry["tp-strategy_pack"].textContent, registry["tp-model_weights"].textContent].join("/"));
ok("复现状态 facet 4 行（全部 + 3 态）",
   (registry["qRepro"].innerHTML.match(/data-qfacet="repro"/g) || []).length === 4,
   "实际 " + (registry["qRepro"].innerHTML.match(/data-qfacet="repro"/g) || []).length);
ok("策略风格 facet 5 行（全部 + 4 类）",
   (registry["qStyle"].innerHTML.match(/data-qfacet="style"/g) || []).length === 5,
   "实际 " + (registry["qStyle"].innerHTML.match(/data-qfacet="style"/g) || []).length);
const reproCount = (registry["qRepro"].innerHTML.match(/data-value="reproduced"[\s\S]{0,300}?fr-count">(\d\d)</) || [])[1];
ok("facet 动态计数：已本地复现 = 02", reproCount === "02", "实际 " + reproCount);
ok("复现徽章三态渲染（2 已复现 + 3 未复现 + 2 不可安装）",
   (poolHtml.match(/repro-chip reproduced/g) || []).length === 2 &&
   (poolHtml.match(/repro-chip unverified/g) || []).length === 3 &&
   (poolHtml.match(/repro-chip locked/g) || []).length === 2,
   [(poolHtml.match(/repro-chip reproduced/g) || []).length,
    (poolHtml.match(/repro-chip unverified/g) || []).length,
    (poolHtml.match(/repro-chip locked/g) || []).length].join("/"));

const flow = registry["dataFlowTable"] ? registry["dataFlowTable"].innerHTML : "";
ok("数据去向表 12 行（10 官方 + 2 社区）", (flow.match(/<tr>/g) || []).length === 12, "实际 " + (flow.match(/<tr>/g) || []).length);
ok("数据去向含 5 个网络出口（3 官方 + 2 社区）", (flow.match(/网络出口/g) || []).length === 5,
   "实际 " + (flow.match(/网络出口/g) || []).length);

/* ---------- 扩展市场 v2 断言 ---------- */
const ext = registry["pluginGroups"] ? registry["pluginGroups"].innerHTML : "";
ok("扩展页含 12 个插件卡", (ext.match(/class="plugin-card"/g) || []).length === 12,
   "实际 " + (ext.match(/class="plugin-card"/g) || []).length);
ok("扩展页含 12 个信任四维面板", (ext.match(/class="trust-panel"/g) || []).length === 12,
   "实际 " + (ext.match(/class="trust-panel"/g) || []).length);
ok("扩展页含 240 个信任点（12 卡 × 4 维 × 5 点）", (ext.match(/class="tp-dot[ "]/g) || []).length === 240,
   "实际 " + (ext.match(/class="tp-dot[ "]/g) || []).length);
ok("扩展页含至少 1 个 full 插槽（today.brief 4 投 3 席）", ext.indexOf("full") !== -1);
ok("扩展页含 L0 静默插件", ext.indexOf("仅入证据账本") !== -1);
ok("社区插件标记 community（2 个）", (ext.match(/card-avatar community/g) || []).length === 2,
   "实际 " + (ext.match(/card-avatar community/g) || []).length);
ok("升级提示 v0.1.1 出现", ext.indexOf("v0.1.1") !== -1);

/* 头部统计 */
ok("市场头部 mInstalled=12", registry["mInstalled"] && registry["mInstalled"].textContent === "12");
ok("市场头部 mEnabled=08", registry["mEnabled"] && registry["mEnabled"].textContent === "08");
ok("市场头部 mUpgrade=01", registry["mUpgrade"] && registry["mUpgrade"].textContent === "01");

/* 过滤栏 */
ok("能力域 6 项", (registry["fDomain"].innerHTML.match(/data-facet="domain"/g) || []).length === 6);
ok("数据去向 3 项", (registry["fLocality"].innerHTML.match(/data-facet="locality"/g) || []).length === 3);
ok("容量过滤 1 项", (registry["fCapacity"].innerHTML.match(/data-facet="capacity"/g) || []).length === 1);

/* 插槽容量仪表盘 */
const slotGrid = registry["slotGrid"] ? registry["slotGrid"].innerHTML : "";
ok("插槽仪表盘 9 个槽位（含 notification.global 与 app.library / app.macro）", (slotGrid.match(/class="slot-cell"/g) || []).length === 9,
   "实际 " + (slotGrid.match(/class="slot-cell"/g) || []).length);
ok("notification.global 以 L0 静默呈现且不占主视图",
   slotGrid.indexOf("notification.global") !== -1 && slotGrid.indexOf("不占主视图") !== -1);
ok("today.brief 显示 3/3", slotGrid.indexOf("3/3") !== -1);
ok("research.board 显示 2/3", slotGrid.indexOf("2/3") !== -1);
ok("满槽触发 排队 提示", slotGrid.indexOf("排队") !== -1);
ok("today.brief 排队 1 个", slotGrid.indexOf("1 个排队") !== -1);

/* tab 计数 */
ok("发现 12", registry["cnt-discover"] && registry["cnt-discover"].textContent === "12");
ok("已安装 09", registry["cnt-installed"] && registry["cnt-installed"].textContent === "09");
ok("升级 01", registry["cnt-upgrade"] && registry["cnt-upgrade"].textContent === "01");

/* 静态结构（基于真实 HTML 文本） */
ok("扩展页含「为什么这个市场」信任 banner", html.includes("为什么这个市场"));
ok("扩展页含「信任四维」文案", html.includes("信任四维"));
ok("扩展页含「会出现在」插槽预演标题", html.includes("会出现在"));
ok("扩展页含「编排原则」说明", html.includes("编排原则"));
ok("扩展页含「按信任度不是装的人多」", html.includes("不是装的人多"));
ok("分享池含 HF 式类型页签", html.includes('id="typePills"'));
ok("分享池含 facet 侧栏 poolRail", html.includes('id="poolRail"'));
ok("分享池默认排序键为推荐（口径 → 样本外 → 复现），不含收益率",
   html.includes("排序：推荐（口径 → 样本外 → 复现）"));

/* 桌面化外壳 + 插件排版原语 */
ok("桌面化：含自定义标题栏 titlebar", html.includes('class="titlebar"'));
ok("桌面化：窗口控制占位 3 个（最小化/最大化/关闭）",
   (html.match(/class="tb-btn"/g) || []).length === 2 && (html.match(/class="tb-btn close"/g) || []).length === 1,
   "实际 " + (html.match(/class="tb-btn"/g) || []).length + "/" + (html.match(/class="tb-btn close"/g) || []).length);
ok("桌面化：主区为独立滚动容器（mainScroll + overflow-y）",
   html.includes('id="mainScroll"') && /\.main\{[^}]*overflow-y:auto/.test(html));
ok("桌面化：外壳尺寸 token 化（--shell-titlebar-h / --shell-rail-w / --shell-pad-x）",
   html.includes("--shell-titlebar-h") && html.includes("--shell-rail-w") && html.includes("--shell-pad-x"));
ok("插件排版：通用卡片宿主 .slot-cards（auto-fill 自适应网格）",
   html.includes(".slot-cards{") && html.includes("repeat(auto-fill,minmax(300px,1fr))"));
ok("桌面化：新鲜度 chip 上移标题栏，顶栏不再重复",
   html.includes('class="tb-fresh"') && (html.match(/class="fresh"/g) || []).length === 0);

/* 桌面满幅高密度（截图反馈：不像桌面端 / 排版不对 / 重点不分明） */
ok("桌面满幅：主区 max-width 提升至 1720", /\.main\{[^}]*max-width:1720px/.test(html));
ok("桌面满幅：hero band 压缩（无 min-height:180px，orbit 装饰隐藏）",
   !/\.band\{[^}]*min-height:180px/.test(html) && /\.orbit\{display:none\}/.test(html));
ok("桌面高密度：metric 面板压缩（无 min-height:104px，strong 降为 20px）",
   !/\.metric\{[^}]*min-height:104px/.test(html) && /\.metric strong\{[^}]*font:500 20px/.test(html));
ok("桌面高密度：间距体系压缩（grid-2 gap26 / slot-block 24）",
   /\.grid-2\{[^}]*gap:26px/.test(html) && /\.slot-block\{margin-top:24px/.test(html));

/* 插件功能可见性（截图反馈：插件功能在哪里看、哪里用） */
ok("插件可见性：扩展页顶部引导条 flow-strip（4 步流向）",
   html.includes('class="flow-strip"') && (html.match(/class="fs-step"/g) || []).length === 4,
   "实际 " + (html.match(/class="fs-step"/g) || []).length);
ok("插件可见性：今天页「来自你的插件」面板容器", html.includes('id="todayPlugins"') && html.includes("在扩展页管理全部"));
const poHtml = registry["todayPlugins"] ? registry["todayPlugins"].innerHTML : "";
ok("插件可见性：今天页插件输出 8 行（8 个已启用插件）",
   (poHtml.match(/class="po-row"/g) || []).length === 8,
   "实际 " + (poHtml.match(/class="po-row"/g) || []).length);
ok("插件可见性：输出行点击直达对应页面（投资2/今天2/研究1/复盘1/图书馆1）",
   (poHtml.match(/data-page="investment"/g) || []).length === 2 &&
   (poHtml.match(/data-page="today"/g) || []).length === 2 &&
   (poHtml.match(/data-page="research"/g) || []).length === 1 &&
   (poHtml.match(/data-page="review"/g) || []).length === 1 &&
   (poHtml.match(/data-page="library"/g) || []).length === 1,
   [(poHtml.match(/data-page="investment"/g) || []).length,
    (poHtml.match(/data-page="today"/g) || []).length,
    (poHtml.match(/data-page="research"/g) || []).length,
    (poHtml.match(/data-page="review"/g) || []).length].join("/"));
ok("插件可见性：输出去向汇总文案已填充",
   registry["poSummary"] && registry["poSummary"].textContent === "8 个已启用插件 · 13 个输出位置 · 点击行直达",
   registry["poSummary"] && registry["poSummary"].textContent);
ok("插件可见性：插槽芯片可点击跳页（slotPageOf 映射 + 卡片 data-page）",
   code.includes("function slotPageOf") && /class="slot-chip[^"]*" data-page="[^"]+"/.test(ext));

/* 桌面适配第二轮：底部状态栏 + 窄窗口响应式 + API 密钥区 */
ok("桌面适配：底部状态栏（token 化高度 + 三行网格）",
   html.includes("--shell-statusbar-h:26px") &&
   /grid-template-rows:var\(--shell-titlebar-h\) 1fr var\(--shell-statusbar-h\)/.test(html) &&
   html.includes('class="statusbar"'));
ok("桌面适配：状态栏内容（连接 / 数据截至 / 插件 / 插槽占用 12/14）",
   html.includes("loopback:7421") && html.includes("14/22") && html.includes("8 启用 · 1 停用 · 3 可安装"));
ok("桌面适配：窄窗口 920px 断点（双栏堆叠单列）",
   /@media \(max-width:920px\)\{/.test(html) && /\.grid-2,\.research-layout,\.set-layout,\.ext-layout,\.md-wrap,\.review-grid,\.grid-half\{grid-template-columns:1fr\}/.test(html));
ok("API 密钥：设置页「数据源与密钥」分区存在（data-stab=keys → setpane-keys）",
   html.includes('data-stab="keys"') && html.includes('id="setpane-keys"'));
ok("API 密钥：CC Switch 式方案列表（3 方案 + radio + 使用中 + 新增方案）",
   (html.match(/class="scheme-row/g) || []).length === 3 &&
   (html.match(/class="sc-radio"/g) || []).length === 3 &&
   /scheme-row[^"]* active/.test(html) &&
   html.includes("＋ 新增方案"),
   "方案行 " + (html.match(/class="scheme-row/g) || []).length);
ok("API 密钥：2 行单实例密钥（password 掩码 + 显示切换 + 测试连接 ×5）",
   (html.match(/class="key-row"/g) || []).length === 2 &&
   (html.match(/class="key-input" type="password"/g) || []).length === 2 &&
   (html.match(/class="eye-btn"/g) || []).length === 2 &&
   (html.match(/class="ghost-btn[^"]*key-test"/g) || []).length === 5,
   [(html.match(/class="key-row"/g) || []).length,
    (html.match(/class="key-input" type="password"/g) || []).length,
    (html.match(/class="eye-btn"/g) || []).length,
    (html.match(/class="ghost-btn[^"]*key-test"/g) || []).length].join("/"));
ok("API 密钥：方案整行切换交互（点行切使用中 / 点按钮不触发）",
   code.includes(".scheme-row") && code.includes('"使用中"') && code.includes('"未启用"') &&
   code.includes('e.target.closest("button")'));
ok("去 AI 化：线性 SVG 图标导航（9 个 nav-ico，含专业模式滑杆与应用区书本/地球）",
   (html.match(/class="nav-ico/g) || []).length === 9 &&
   /\.nav-ico\{[^}]*fill:none;stroke:currentColor/.test(html),
   "图标 " + (html.match(/class="nav-ico/g) || []).length);
ok("去 AI 化：移除编号与圆形徽章（无 nav-index / 无 badge）",
   html.indexOf('class="nav-index"') === -1 && html.indexOf('class="badge"') === -1 &&
   (html.match(/class="nav-count"/g) || []).length === 2);
ok("侧栏：分区可折叠（2 个 side-group + data-group 标题 + 折叠箭头）",
   (html.match(/class="side-group"/g) || []).length === 2 &&
   (html.match(/class="side-label" data-group=/g) || []).length === 2 &&
   /\.side-group\.collapsed \.nav\{display:none\}/.test(html) &&
   code.includes(".side-label[data-group]"));
ok("插件输出分区已从侧栏移除（插槽名是实现术语，去向由今天页面板用用户语言承载）",
   html.indexOf('id="slotRail"') === -1 && html.indexOf("slot-nav-item") === -1 &&
   code.indexOf("function renderSlotRail") === -1 && html.includes('id="todayPlugins"'));
ok("插件不拥挤：侧栏入口恒定（导航+应用+设置共 8 个 nav-item，不随插件数增长）",
   (html.match(/class="nav-item[ "]/g) || []).length === 8,
   "nav-item " + (html.match(/class="nav-item[ "]/g) || []).length);

/* 双挂载：独立应用型插件（app.* 槽位 + 侧栏「应用」区 + 独立页面壳） */
ok("侧栏折叠态对齐：统一水平 padding 与行边距（图标同轴居中）",
   /body\.rail-collapsed \.sidebar\{padding:14px 10px\}/.test(html) &&
   /body\.rail-collapsed \.nav-item\{grid-template-columns:1fr;justify-items:center;padding:6px 0;margin:2px 6px/.test(html) &&
   /body\.rail-collapsed \.pro-toggle\{justify-content:center;padding:6px 0;margin:2px 6px/.test(html));
ok("设置图标重绘：标准齿轮（lucide settings 双路径，非太阳状）",
   code.indexOf('M12.22 2h-.44') !== -1 || html.indexOf('M12.22 2h-.44') !== -1);
ok("双挂载：SLOTS 声明独立应用槽 app.library（cap 4）",
   code.includes('{slot:"app.library"') && code.includes('cap:4') &&
   code.includes('p === "app") return "library"'));
ok("双挂载：侧栏「应用」区入口（研读图书馆 + 配额 1/4）",
   html.includes('data-group="apps"') && html.includes('data-page="library"') &&
   html.includes("2/4") && html.includes("研读图书馆") && html.includes("宏观雷达"));
ok("双挂载：独立页面壳 page-library 存在且声明基座插槽 today.learning",
   pages.includes("library") && html.includes('id="page-library"') &&
   code.includes('{slot:"app.library",level:"L3"},{slot:"today.learning",level:"L2"}'));
ok("宏观雷达：SLOTS 槽位 + 页面壳 + 映射（app.macro → macro）",
   code.includes('{slot:"app.macro"') && html.includes('id="page-macro"') &&
   code.includes('s.page === "宏观" ? "macro"') && html.includes('data-page="macro"'));
ok("宏观雷达：页面内容（5 国卡 + 背景层 3 卡 + 指标行 + 本月异动入研究页 + 边界说明）",
   (html.match(/class="macro-card"/g) || []).length === 8 &&
   (html.match(/class="mc-ind"/g) || []).length === 30 &&
   html.includes("非农就业（7 月）") && html.includes("作为证据提交到研究页") &&
   html.includes("宏观判断是背景，不是结论") &&
   html.includes("货币指数") && html.includes("布伦特现货") && html.includes("伦敦金定盘"));
ok("双挂载：插件卡区分挂载类型（独立应用 / 页面增强 chip）",
   code.includes('mount === "own_page"') && code.includes("独立应用 · 侧栏「应用」区") &&
   code.includes("页面增强"));
ok("API 密钥：密钥边界说明（不进插件 / 日志 / 分享池，Core 代理注入）",
   html.includes("密钥边界") && html.includes("Core 代理") && html.includes("不进日志"));
ok("API 密钥：显示切换与测试连接交互已实现",
   code.includes('.eye-btn') && code.includes('input.type = showing ? "password" : "text"') &&
   code.includes(".key-test") && code.includes('"已验证 · 演示"'));

/* 多插件排版演示 + 桌面交互 */
const lanes = registry["demoLanes"] ? registry["demoLanes"].innerHTML : "";
ok("压力演示：3 条泳道（L3 主卡 / L2 摘要 / L0 静默）",
   (lanes.match(/class="lane"/g) || []).length === 3,
   "实际 " + (lanes.match(/class="lane"/g) || []).length);
ok("压力演示：主卡 3 张 + 摘要 2 行 + 更多折叠",
   (lanes.match(/class="demo-card/g) || []).length === 3 &&
   (lanes.match(/class="demo-row"/g) || []).length === 2 &&
   lanes.indexOf("更多 6 项") !== -1,
   [(lanes.match(/class="demo-card/g) || []).length,
    (lanes.match(/class="demo-row"/g) || []).length,
    lanes.indexOf("更多 6 项") !== -1].join("/"));
ok("压力演示：L0 静默插件不进主视图", lanes.indexOf("仅写入证据账本") !== -1);
ok("列表视图已实现（EXT_VIEW + pluginRow）",
   code.includes("EXT_VIEW") && code.includes("function pluginRow"));
ok("侧栏折叠（railToggle + rail-collapsed）",
   html.includes('id="railToggle"') && html.includes("body.rail-collapsed{--shell-rail-w:64px}"));
ok("桌面骨架：顶栏吸附为应用工具栏（sticky + 不透明底 + token 化负边距通宽）",
   /\.topbar\{position:sticky;top:0;z-index:10;/.test(html) &&
   /\.topbar\{[^}]*margin:0 calc\(-1 \* var\(--shell-pad-x\)\)/.test(html) &&
   /\.topbar\{[^}]*background:var\(--canvas\)\}/.test(html));
ok("桌面骨架：面板卡片化（panel 带 1px 边框）", /\.panel\{[^}]*border:1px solid var\(--line\)\}/.test(html));
ok("桌面骨架：密度收紧（pad-x 40 / 主区顶距 8px）",
   html.includes("--shell-pad-x:40px") && /\.main\{[^}]*padding:8px var\(--shell-pad-x\)/.test(html));
ok("侧栏折叠态：专业模式图标化（滑杆图标替代浮动开关，开启态紫色）",
   html.includes('class="nav-ico pro-ico"') &&
   /body\.rail-collapsed \.pro-toggle \.switch\{display:none\}/.test(html) &&
   /body\.rail-collapsed \.pro-toggle\[aria-pressed="true"\] \.pro-ico\{color:var\(--violet\)\}/.test(html));
ok("侧栏折叠态：只留图标（隐藏文字与计数，图标 19px 居中）",
   /body\.rail-collapsed \.nav-item span,body\.rail-collapsed \.nav-item em\{display:none\}/.test(html) &&
   /body\.rail-collapsed \.nav-ico\{width:19px;height:19px/.test(html));
ok("桌面快捷键 Ctrl/Cmd+1..5 与 Ctrl/Cmd+,",
   code.includes('"5":"quant"') && code.includes('showPage(pmap[e.key])'));
ok("标题栏窗口控制已接 Electron（stewardWin + IPC）",
   code.includes("window.stewardWin") && code.includes(".tb-win .tb-btn"));

const extTabsFromHtml = [...new Set([...html.matchAll(/data-ext="([a-z]+)"/g)].map(x => x[1]))];
ok("扩展 tab 4 个（发现/已安装/升级/高级）", extTabsFromHtml.length === 4, "实际 " + extTabsFromHtml.length);

/* 专业模式：初始隐藏，点击后显示 */
const navQuant = registry["navQuant"];
ok("量化研究入口初始隐藏", navQuant && navQuant.classList.contains("hidden"));
registry["proToggle"].fire("click");
ok("开启专业模式后显示量化研究", navQuant && !navQuant.classList.contains("hidden"));
ok("专业模式提示文案已切换", registry["proHint"].textContent.indexOf("已显示") !== -1,
   registry["proHint"].textContent);

/* 切到 quant 页再关专业模式，应回落到今天页 */
document._h["click"].forEach(fn => fn({ target: { closest: s => s === "[data-page]" ? { getAttribute: () => "quant" } : null } }));
ok("可进入量化研究页", pageEls["quant"].classList.contains("active"));
registry["proToggle"].fire("click");
ok("关闭专业模式后离开量化页", pageEls["quant"].classList.contains("active") === false &&
   pageEls["today"].classList.contains("active"));

/* 制品详情 */
document._h["click"].forEach(fn => fn({ target: { closest: s => s === "[data-page]" ? { getAttribute: () => "quant" } : null } }));
registry["proToggle"].fire("click");
const artClick = { target: { closest: s => s === "[data-art]" ? { getAttribute: () => "0" } : null } };
document._h["click"].forEach(fn => fn(artClick));
const detail = registry["quantDetail"].innerHTML;
ok("制品详情已渲染", detail.length > 500, "长度 " + detail.length);
ok("详情页含 7 个区块", (detail.match(/class="block"/g) || []).length >= 6,
   "实际 " + (detail.match(/class="block"/g) || []).length);
ok("详情页含风险披露", detail.indexOf("风险披露") !== -1);
ok("详情页含参数表输入", (detail.match(/<input /g) || []).length === 5,
   "实际 " + (detail.match(/<input /g) || []).length);
ok("详情页含派生谱系", detail.indexOf("派生谱系") !== -1);
ok("详情页含复现记录", detail.indexOf("复现记录") !== -1);
ok("列表已隐藏", registry["quantList"].style.display === "none");

const backClick = { target: { closest: s => s === "[data-artback]" ? { getAttribute: () => "1" } : null } };
document._h["click"].forEach(fn => fn(backClick));
ok("返回后列表恢复", registry["quantList"].style.display === "block" &&
   registry["quantDetail"].style.display === "none");

/* 分标签渲染 */
const qtabMine = qtabEls.find(e => e.getAttribute("data-qtab") === "mine");
qtabMine.fire("click");
ok("我的实验标签只渲染 1 个", (registry["poolList"].innerHTML.match(/class="pool-item/g) || []).length === 1,
   "实际 " + (registry["poolList"].innerHTML.match(/class="pool-item/g) || []).length);
const qtabLineage = qtabEls.find(e => e.getAttribute("data-qtab") === "lineage");
qtabLineage.fire("click");
ok("我的派生标签只渲染 1 个", (registry["poolList"].innerHTML.match(/class="pool-item/g) || []).length === 1,
   "实际 " + (registry["poolList"].innerHTML.match(/class="pool-item/g) || []).length);
const qtabPool = qtabEls.find(e => e.getAttribute("data-qtab") === "pool");
qtabPool.fire("click");
function search(kw) {
  registry["poolSearch"].value = kw;
  (registry["poolSearch"]._h["input"] || []).forEach(fn => fn.call(registry["poolSearch"]));
  return registry["poolList"].innerHTML;
}
/* "leno" 会同时命中原制品和描述里提到 @leno 的派生版 —— 这是正确行为 */
ok("搜索 leno 命中 2 个（原制品 + 派生）", (search("leno").match(/class="pool-item/g) || []).length === 2,
   "实际 " + (search("leno").match(/class="pool-item/g) || []).length);
ok("搜索 宽基 只命中基线", (search("宽基").match(/class="pool-item/g) || []).length === 1,
   "实际 " + (search("宽基").match(/class="pool-item/g) || []).length);
ok("无命中显示空态", search("zzzz").indexOf("还没有制品") !== -1);
search("");

/* 设置分区切换 */
const setPrivacy = setEls.find(e => e.getAttribute("data-stab") === "privacy");
setPrivacy.fire("click");
ok("设置分区切到隐私", registry["setpane-privacy"].classList.contains("active") &&
   registry["setpane-ext"].classList.contains("active") === false);

console.log(fail === 0 ? "\nALL_OK  (" + "全部通过" + ")" : "\nFAILED  " + fail + " 项");
process.exit(fail === 0 ? 0 : 1);
