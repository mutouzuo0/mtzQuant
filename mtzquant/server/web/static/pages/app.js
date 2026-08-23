/* coding:utf-8
 * @author      : 木头左
 * @create_time : 2026/08/17 03:20:00
 * @update_time : 2026/08/23 12:00:00
 * @description : mtzQuant Web 页面逻辑（原生 JS 无构建链, 9.1 数据一律来自 REST/DB 同源）——
 *                连接三态(P1-1)/状态徽章全量(P1-3)/指标缺失原因+补算(P0-2)/覆盖语义(P2-2)/
 *                表单增强(P2-4)/历史监控订阅+报告一键重跑+成交明细(横切)。
 *                注意: 不声明 $/fmt/fmtPct 等名字（与 index.html 内联脚本顶层 const 冲突）。
 */

"use strict";

// Y1 认证: token 存 localStorage（13.5）; 401 时提示录入（Watcher/Operator 同权校验）
const TOKEN_KEY = "mtzquant_token";
function apiHeaders() {
  const t = localStorage.getItem(TOKEN_KEY);
  return t ? { "Content-Type": "application/json", "Authorization": "Bearer " + t } : { "Content-Type": "application/json" };
}

const API = {
  async _req(path, opts) {
    const r = await fetch(path, opts);
    if (r.status === 401) {
      const t = window.prompt("需要认证 token（secrets server.tokens, 13.5）:");
      if (t) {
        localStorage.setItem(TOKEN_KEY, t.trim());
        return this._req(path, opts);
      }
    }
    if (!r.ok) { const e = await r.json().catch(()=>({})); throw new Error(e.detail || r.status); }
    return r.json();
  },
  async get(path) { return this._req(path, { headers: apiHeaders() }); },
  async post(path, body) {
    return this._req(path, { method: "POST", headers: apiHeaders(),
      body: body ? JSON.stringify(body) : undefined });
  },
  async del(path) {
    return this._req(path, { method: "DELETE", headers: apiHeaders() });
  }
};

const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
const el = id => document.getElementById(id);

/* ---- 历史/报告共用格式化（app.js 独立持有, 避免跨脚本时序耦合） ---- */
const pct = v => v == null ? "—" : (v * 100).toFixed(2) + "%";
const num2 = v => v == null ? "—" : Number(v).toFixed(2);
const fmtTime = s => s ? String(s).slice(0, 19).replace("T", " ") : "—";

/* 状态中文化 + 配色 + 解释（P1-3: 覆盖全部状态, hover 展示原始码与说明） */
const STATUS_META = {
  running: ["运行中", "s-running", "回测正在执行"],
  paused: ["已暂停", "s-paused", "回测已暂停"],
  completed_exact: ["成功", "s-completed_exact", "回测完整完成，无数据降级"],
  completed_degraded: ["成功·降级", "s-completed_degraded",
    "回测完成，但有数据缺失/适配器降级，详见报告中的降级清单"],
  stopped: ["已终止", "s-stopped", "回测被手动终止"],
  error: ["失败", "s-error", "回测异常终止，详见错误日志"],
  dry_run: ["覆盖检查", "", "只读本地数据估算缺口，未联网下载"],
  ok: ["成功", "s-completed_exact", "数据下载完成"],
  skipped: ["跳过", "", "本地已覆盖请求区间，无需下载"],
  partial: ["部分成功", "s-paused", "部分标的成功，部分失败"],
  failed: ["失败", "s-error", "操作失败，详见说明"]
};
function statusBadge(st) {
  const m = STATUS_META[st] || [st || "—", "", "未知状态"];
  return `<span class="pill ${m[1]}" title="${esc(st)} — ${esc(m[2])}">${m[0]}</span>`;
}

/* ==================== 新建回测页（M4 表单化） ==================== */
window.mtzNew = {
  jsonMode: false,   // true: JSON 文本域为提交源; false: 表单字段为源（文本域仅预览）
  nameDirty: false,  // 用户手改过任务名后不再自动生成

  init() {
    // 默认区间: 去年 1 月 1 日 ~ 本月 1 日（用户规格）; 频率固定日线
    const now = new Date();
    el("newStart").value = `${now.getFullYear() - 1}-01-01`;
    el("newEnd").value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
    this.echoDateDefaults();
    this.fmtCapital();
    el("newPlatform").onchange = () => this.loadStrategies();
    el("newStrategy").onchange = () => this.autoName();
    el("btnRefreshStrategies").onclick = () => this.loadStrategies();
    el("newTaskName").oninput = () => { this.nameDirty = !!el("newTaskName").value; this.preview(); };
    ["newCapital", "newBenchmark", "newStart", "newEnd"].forEach(id => { el(id).oninput = () => { this.preview(); }; });
    el("newCapital").oninput = () => { this.fmtCapital(); this.preview(); };
    ["feeCommRate", "feeCommMin", "feeStamp", "feeTransfer"].forEach(id => {
      el(id).oninput = () => this.preview();
    });
    el("newStrategyFile").onchange = e => this.upload(e);
    el("btnToggleJson").onclick = () => this.toggleJson();
    this.loadStrategies();
  },

  // P2-4: 默认区间回显到提示文案（用户能确认将回测的区间）
  echoDateDefaults() {
    const h = el("dateHint");
    if (h) h.textContent = `已填入默认区间（可修改）: ${el("newStart").value} ~ ${el("newEnd").value}`;
  },

  // P2-4: 初始资金千分位展示（输入框保持数字, 旁侧格式化）
  fmtCapital() {
    const v = Number(el("newCapital").value);
    const c = el("capFmt");
    if (c) c.textContent = Number.isFinite(v) && v ? `= ${v.toLocaleString("zh-CN")} 元` : "";
  },

  async loadStrategies() {
    const platform = el("newPlatform").value, sel = el("newStrategy");
    sel.innerHTML = `<option value="">（加载中…）</option>`;
    try {
      const list = await API.get(`/api/strategies?platform=${platform}`);
      sel.innerHTML = list.length
        ? list.map(s => `<option value="${esc(s.path)}">${esc(s.name)}（${(s.size / 1024).toFixed(1)}K）</option>`).join("")
        : `<option value="">（该平台暂无策略, 请上传 .py）</option>`;
    } catch (e) {
      sel.innerHTML = `<option value="">（加载失败: ${esc(e.message)}）</option>`;
    }
    this.autoName();
    this.preview();
  },

  autoName() {
    if (this.nameDirty) { this.preview(); return; }
    const f = el("newStrategy").value || "";
    const base = (f.split("/").pop() || "strategy").replace(/\.py$/, "");
    el("newTaskName").value = `${base}_${new Date().toISOString().slice(0, 10)}`;
    this.preview();
  },

  async upload(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const msg = el("newUploadMsg");
    msg.textContent = `上传中: ${file.name}…`;
    let code;
    try { code = await file.text(); }
    catch (err) { msg.textContent = "✘ 本地读取失败"; toast("策略文件读取失败", false); return; }
    const platform = el("newPlatform").value, filename = file.name;
    const send = overwrite => API.post("/api/strategies/upload", { platform, filename, code, overwrite });
    try {
      let j;
      try { j = await send(false); }
      catch (err) {  // 409 同名 → 确认后覆盖
        if (/409|overwrite|已存在/.test(String(err.message))) {
          if (confirm(`已存在同名文件: strategies/${platform}/${filename}\n覆盖？`)) j = await send(true);
          else { msg.textContent = "已取消上传"; e.target.value = ""; return; }
        } else throw err;
      }
      msg.textContent = `✔ 已上传: ${j.path}${j.overwritten ? "（覆盖）" : ""}`;
      toast(`策略已上传: ${filename}`);
      await this.loadStrategies();
      el("newStrategy").value = `strategies/${platform}/${filename}`;
      this.autoName();
    } catch (err) {
      msg.textContent = "✘ " + err.message;
      toast("上传失败: " + err.message, false);
    }
    e.target.value = "";
  },

  buildTask() {
    // 标的池不手填: 留空由后端从策略源码自动提取（preflight.detect_codes）; 高级 JSON 模式仍可显式指定
    const task = {
      task_name: el("newTaskName").value.trim() || "web_submit",
      strategy: { file: el("newStrategy").value || "", type: el("newPlatform").value },
      backtest: {
        start: el("newStart").value,
        end: el("newEnd").value,
        initial_capital: Number(el("newCapital").value) || 1000000,
        frequency: "1d"
      }
    };
    const bench = el("newBenchmark").value.trim();
    if (bench) task.backtest.benchmark = bench;
    // P2-4: 高级参数——费率（留空 → 后端品种默认）
    const fees = {};
    [["feeCommRate", "commission_rate"], ["feeCommMin", "min_commission"],
     ["feeStamp", "stamp_tax_rate"], ["feeTransfer", "transfer_fee_rate"]].forEach(([id, k]) => {
      const v = el(id).value;
      if (v !== "" && v != null && Number.isFinite(Number(v))) fees[k] = Number(v);
    });
    if (Object.keys(fees).length) task.fees = fees;
    return task;
  },

  // 横切-3: 用历史 run 的参数回填表单（报告页「用相同参数重跑」）
  async prefill(task) {
    try {
      if (this.jsonMode) this.toggleJson();  // 先回到表单模式
      this.nameDirty = true;
      el("newTaskName").value = (task.task_name || "") + "_rerun";
      if (task.strategy) {
        const plat = task.strategy.type || "ptrade";
        if (["ptrade", "joinquant"].includes(plat)) el("newPlatform").value = plat;
        await this.loadStrategies();
        if (task.strategy.file) el("newStrategy").value = task.strategy.file;
      }
      const b = task.backtest || {};
      if (b.start) el("newStart").value = b.start;
      if (b.end) el("newEnd").value = b.end;
      if (b.initial_capital != null) el("newCapital").value = b.initial_capital;
      if (b.benchmark) el("newBenchmark").value = b.benchmark;
      // 标的池已由后端从策略源码自动提取, 表单不再手填（JSON 模式仍可显式指定）
      if (task.fees) {
        el("feeCommRate").value = task.fees.commission_rate ?? "";
        el("feeCommMin").value = task.fees.min_commission ?? "";
        el("feeStamp").value = task.fees.stamp_tax_rate ?? "";
        el("feeTransfer").value = task.fees.transfer_fee_rate ?? "";
      }
      this.echoDateDefaults();
      this.fmtCapital();
      this.preview();
      location.hash = "#/new";
      toast("已用该 run 的参数回填表单，可调整后重新提交");
    } catch (e) {
      toast("回填参数失败: " + e.message, false);
    }
  },

  validate(task) {
    if (!task.strategy.file) return "请先选择策略文件（或上传 .py）";
    if (!task.backtest.start || !task.backtest.end) return "回测起止日期不完整";
    if (task.backtest.start >= task.backtest.end) return "开始日期须早于结束日期";
    if (!(task.backtest.initial_capital > 0)) return "初始资金须大于 0";
    return "";
  },

  preview() {
    if (this.jsonMode) return;  // JSON 模式下文本域是数据源, 不被表单覆盖
    el("newTaskJson").value = JSON.stringify(this.buildTask(), null, 2);
  },

  toggleJson() {
    this.jsonMode = !this.jsonMode;
    el("newTaskJson").readOnly = !this.jsonMode;
    el("btnToggleJson").textContent = this.jsonMode ? "返回表单模式（以 JSON 提交）" : "高级：JSON 模式";
    el("newJsonBox").style.display = "";
    if (!this.jsonMode) this.preview();  // 回到表单模式 → 按表单重建
    else toast("JSON 模式: 直接编辑文本域提交");
  },

  async loadTask() {
    const path = el("newTask").value.trim();
    const msg = el("newMsg");
    if (!path) { msg.textContent = "请输入任务 JSON 路径"; return; }
    try {
      const j = await API.get(`/api/read-task?path=${encodeURIComponent(path)}`);
      if (!this.jsonMode) this.toggleJson();  // 读入后切 JSON 模式, 避免被表单预览覆盖
      el("newTaskJson").value = JSON.stringify(j, null, 2);
      msg.textContent = "已读取: " + path;
    } catch (e) {
      msg.textContent = "读取失败: " + e.message;
    }
  },

  async submit() {
    const msg = el("newMsg");
    let task;
    if (this.jsonMode) {
      try { task = JSON.parse(el("newTaskJson").value); }
      catch (e) { msg.textContent = "任务 JSON 解析失败: " + e.message; toast("JSON 解析失败", false); return; }
    } else {
      task = this.buildTask();
      const err = this.validate(task);
      if (err) { msg.textContent = "✘ " + err; toast(err, false); return; }
    }
    msg.textContent = "提交中…";
    try {
      const j = await API.post("/api/backtests", task);
      msg.textContent = "✔ 已提交: " + j.run_id;
      toast(`回测已提交, 正在跳转监控: ${j.run_id}`);
      location.hash = "#/monitor?run=" + j.run_id;
    } catch (e) { msg.textContent = "✘ " + e.message; toast("提交失败: " + e.message, false); }
  }
};

/* ==================== 历史 / 对比页（M4: KPI 列 + 筛选排序 + 对比校验） ==================== */
window.mtzHistory = {
  _selected: new Set(),
  _rows: [],
  _filter: "all",
  _sort: { key: "started_at", dir: -1 },
  PLATFORM_CN: { ptrade: "PTrade", joinquant: "聚宽", native: "native" },

  async load() {
    const tb = document.querySelector("#runsTable tbody");
    try {
      this._rows = await API.get("/api/runs?limit=100");
    } catch (e) {
      tb.innerHTML = `<tr><td colspan="11">✘ ${esc(e.message)}</td></tr>`;
      return;
    }
    this.render();
  },

  _match(r) {
    const f = this._filter, st = r.status || "";
    if (f === "running") return st === "running" || st === "paused";
    if (f === "completed") return st.startsWith("completed");
    if (f === "failed") return st === "error";
    if (f === "stopped") return st === "stopped";
    return true;
  },

  render() {
    const q = (el("histSearch").value || "").toLowerCase();
    const { key, dir } = this._sort;
    const rows = this._rows
      .filter(r => this._match(r) &&
        (!q || (r.task_name || "").toLowerCase().includes(q) || (r.run_id || "").toLowerCase().includes(q)))
      .slice()
      .sort((a, b) => {
        const va = a[key], vb = b[key];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;   // 指标缺失（运行中）恒沉底
        if (vb == null) return -1;
        if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
        return String(va).localeCompare(String(vb)) * dir;
      });
    const tb = document.querySelector("#runsTable tbody");
    tb.innerHTML = rows.length ? rows.map(r => this._rowHtml(r)).join("")
      : `<tr><td colspan="10" class="muted">暂无符合条件的 run</td></tr>`;
    tb.querySelectorAll(".run-check").forEach(cb => { cb.onchange = () => this._toggleCheck(cb); });
    tb.querySelectorAll("a[data-logs]").forEach(a => {
      a.onclick = ev => { ev.preventDefault(); viewLogs(a.dataset.logs); };
    });
    tb.querySelectorAll("a[data-source]").forEach(a => {
      a.onclick = ev => { ev.preventDefault(); viewSource(a.dataset.source); };
    });
    tb.querySelectorAll("a[data-del]").forEach(a => {
      a.onclick = ev => { ev.preventDefault(); deleteRun(a.dataset.del); };
    });
    this._updateCompareBtn();
  },

  _rowHtml(r) {
    const done = (r.status || "").startsWith("completed");
    const rid = encodeURIComponent(r.run_id);
    const metricsMissing = done && r.total_return == null && r.annual_return == null
      && r.max_drawdown == null && r.sharpe == null;
    const missTitle = metricsMissing
      ? ` title="${esc(r.metrics_note || "该 run 未写入绩效指标（旧版引擎或未落库），可点「报告」查看或重跑")}"`
      : "";
    const cell = (v, cls = "") => (metricsMissing && v == null)
      ? `<td class="num"${missTitle} style="cursor:help">—</td>`
      : `<td class="num ${cls}">${v}</td>`;
    return `<tr>
      <td><input type="checkbox" class="run-check" data-id="${esc(r.run_id)}" data-plat="${esc(r.platform || "")}"
        ${this._selected.has(r.run_id) ? "checked" : ""} ${done ? "" : 'disabled title="仅已完成的 run 可对比"'}></td>
      <td>${esc(r.task_name)}</td>
      <td>${esc(this.PLATFORM_CN[r.platform] || r.platform || "—")}</td>
      <td>${statusBadge(r.status)}</td>
      ${cell(r.total_return == null ? null : pct(r.total_return), (r.total_return || 0) >= 0 ? "up" : "down")}
      ${cell(r.annual_return == null ? null : pct(r.annual_return))}
      ${cell(r.max_drawdown == null ? null : pct(r.max_drawdown))}
      ${cell(r.sharpe == null ? null : num2(r.sharpe))}
      <td class="muted">${fmtTime(r.started_at)}</td>
      <td>
        <a href="#/report?run=${rid}">报告</a>
        · <a href="#/monitor?run=${rid}" title="订阅该 run 的实时事件流">监控</a>
        · <a href="#" data-logs="${esc(r.run_id)}" title="查看该 run 运行日志">日志</a>
        · <a href="#" data-source="${esc(r.run_id)}" title="查看该 run 实际运行的策略源码快照">源码</a>
        · <a href="#" data-del="${esc(r.run_id)}" class="danger" title="软删除该 run 并移除结果产物">删除</a>
      </td>
    </tr>`;
  },

  _toggleCheck(cb) {
    if (cb.checked) {
      this._selected.add(cb.dataset.id);
      if (this._selected.size > 6) {  // 前置拦截（10.3 上限）
        this._selected.delete(cb.dataset.id);
        cb.checked = false;
        toast("对比最多勾选 6 个 run", false);
      }
    } else {
      this._selected.delete(cb.dataset.id);
    }
    this._updateCompareBtn();
  },

  _updateCompareBtn() {
    const b = el("btnCompare");
    b.textContent = `对比选中 (${this._selected.size}/6)`;
    b.disabled = this._selected.size < 2;
  },

  async compare() {
    const ids = [...this._selected];
    if (ids.length < 2) return;
    const plats = new Set(this._rows.filter(r => ids.includes(r.run_id)).map(r => r.platform));
    if (plats.size > 1 &&
      !confirm("勾选的 run 跨平台（指标口径一致但策略语义不同）, 仍要对比？")) return;
    const box = el("compareBox");
    box.style.display = "block";
    try {
      const root = ids[0];
      const q = ids.slice(1).join(",");
      const j = await API.get(`/api/runs/${root}/compare?ids=${encodeURIComponent(q)}`);
      const thead = document.querySelector("#compareTable thead");
      thead.innerHTML = "<tr><th>指标</th>" + j.runs.map(r=>`<th>${esc(r.slice(0,12))}</th>`).join("") + "</tr>";
      const tbody = document.querySelector("#compareTable tbody");
      tbody.innerHTML = Object.keys(j.rows).map(k => {
        const cells = j.runs.map(r => {
          const v = j.rows[k][r];
          const best = j.best[k] === r;
          return `<td class="num" style="${best?"color:#2ecc71;font-weight:600":""}">${v==null?"—":Number(v).toFixed(4)}</td>`;
        });
        return `<tr><td>${esc(k)}</td>${cells.join("")}</tr>`;
      }).join("");
      drawCompareNav(root, q);
    } catch (e) {
      document.querySelector("#compareTable tbody").innerHTML = `<tr><td>✘ ${esc(e.message)}</td></tr>`;
    }
  }
};

/* ==================== 通用弹窗 + 历史操作（日志/源码/删除） ==================== */
function openModal(title, html, actionsHtml = "") {
  el("modalTitle").textContent = title;
  el("modalBody").innerHTML = html;
  el("modalActions").innerHTML = actionsHtml;
  el("modal").style.display = "block";
}
function closeModal() {
  const m = el("modal");
  if (m) m.style.display = "none";
}
async function viewLogs(runId) {
  openModal("运行日志", `<div id="logLines">加载中…</div>`, `<button id="btnLogReload">刷新</button>`);
  const render = async () => {
    const box = el("logLines");
    if (!box) return;
    box.textContent = "加载中…";
    try {
      const j = await API.get(`/api/runs/${encodeURIComponent(runId)}/logs`);
      const lines = (j.logs || []).map(l => {
        const ts = l.current_dt
          ? String(l.current_dt).slice(0, 19).replace("T", " ")
          : (l.ts ? new Date(l.ts).toLocaleString("zh-CN") : "");
        const tag = String(l.level || "info").toLowerCase();
        const cls = tag === "error" ? "log-err" : (tag === "warn" || tag === "warning") ? "log-warn" : "";
        const label = l.type === "status" ? "状态" : (l.level || "info");
        return `<div class="${cls}">[${esc(ts)}] [${esc(label)}] ${esc(l.message)}</div>`;
      });
      if (!lines.length) lines.push(`<div class="muted">（该 run 暂无日志）</div>`);
      if (j.error_log) lines.push(`<div class="log-err">[error_log] ${esc(j.error_log)}</div>`);
      box.innerHTML = lines.join("");
    } catch (e) {
      box.textContent = "✘ 加载日志失败: " + e.message
        + (/not found|404/i.test(e.message) ? "（端点需重启 mtzquant serve 生效）" : "");
    }
  };
  render();
  const b = el("btnLogReload");
  if (b) b.onclick = render;
}
async function viewSource(runId) {
  openModal("原策略源码", `<pre>加载中…</pre>`,
    `<button id="btnCopyCode">复制</button><button id="btnDlCode">下载 .py</button>`);
  try {
    const j = await API.get(`/api/runs/${encodeURIComponent(runId)}/source`);
    el("modalTitle").textContent = `原策略源码 · ${j.file_name || runId}`;
    const pre = el("modalBody").querySelector("pre");
    pre.textContent = j.code || "（空源码）";
    const copy = el("btnCopyCode");
    if (copy) copy.onclick = () => {
      if (navigator.clipboard) navigator.clipboard.writeText(j.code || "");
      toast("源码已复制");
    };
    const dl = el("btnDlCode");
    if (dl) dl.onclick = () => {
      const a = document.createElement("a");
      a.href = "data:text/plain;charset=utf-8," + encodeURIComponent(j.code || "");
      a.download = (j.file_name || "strategy.py").split("/").pop();
      document.body.appendChild(a);
      a.click();
      a.remove();
    };
  } catch (e) {
    const pre = el("modalBody").querySelector("pre");
    pre.textContent = "✘ 加载源码失败: " + e.message
      + (/not found|404/i.test(e.message) ? "（端点需重启 mtzquant serve 生效）" : "");
  }
}
async function deleteRun(runId) {
  if (!confirm(`确定删除该回测？\n${runId}\n（DB 记录软删隐藏 + 移除 results 产物目录, 可重跑复现）`)) return;
  try {
    await API.del(`/api/runs/${encodeURIComponent(runId)}`);
    toast("✔ 已删除: " + runId);
    window.mtzHistory._selected.delete(runId);
    await window.mtzHistory.load();
  } catch (e) {
    toast("✘ 删除失败: " + e.message, false);
  }
}

async function drawCompareNav(root, others) {
  const navs = await Promise.all([root, ...others.split(",").filter(Boolean)].map(async rid => {
    try { return { rid, rows: await API.get(`/api/runs/${rid}/navs`) }; } catch (e) { return { rid, rows: [] }; }
  }));
  const chart = echarts.init(document.getElementById("compareChart"));
  chart.setOption({
    grid:{left:50,right:20,top:20,bottom:40},
    tooltip:{trigger:"axis"},
    legend:{data:navs.map(n=>n.rid.slice(0,14)), textStyle:{color:"#8b96b0"}},
    xAxis:{type:"time",axisLabel:{color:"#8b96b0"}},
    yAxis:{type:"value",scale:true,axisLabel:{color:"#8b96b0"}},
    series: navs.map(n => ({
      name: n.rid.slice(0,14), type:"line", showSymbol:false,
      data: n.rows.map(r => [new Date(r.trade_date+"T00:00:00+08:00").getTime(), r.strategy_nav]),
      lineStyle:{width:1.4}
    }))
  });
}

/* ==================== 报告详情页（M4: Web 端 report.html, 三面同权 9.1） ==================== */
window.mtzReport = {
  _runId: "",
  load() {
    const runId = new URLSearchParams(location.hash.split("?")[1] || "").get("run") || "";
    this._runId = runId;
    const frame = el("repFrame");
    const msg = el("repMsg");
    frame.style.display = "";
    if (msg) { msg.textContent = ""; msg.style.color = ""; }
    el("repRunId").textContent = runId || "（缺 run 参数）";
    el("repBack").onclick = () => { location.hash = "#/history"; };
    if (!runId) { if (msg) msg.textContent = "缺少 run 参数：请从「历史」页进入报告"; return; }
    const url = `/api/runs/${encodeURIComponent(runId)}/report`;
    el("repOpen").href = url;
    el("repReload").onclick = () => { frame.src = url + "?t=" + Date.now(); };
    // P0-1: 报告加载显式反馈（不再静默空白）——run 存在性 + 状态由 API 权威判定
    API.get(`/api/runs/${runId}`).then(d => {
      const m = STATUS_META[d.status] || [d.status || "—", "", ""];
      el("repStatus").className = "pill " + m[1];
      el("repStatus").textContent = m[0];
      el("repStatus").title = `${d.status} — ${m[2]}`;
      // 横切-3: 一键重跑——用该 run 参数回填新建表单（params 即脱敏后的 task json）
      el("repRerun").onclick = () => {
        const task = d.params;
        if (task && Array.isArray(task.universe)) mtzNew.prefill(task);
        else toast("该 run 无可用参数（旧记录）, 请手动新建", false);
      };
    }).catch(e => {
      if (msg) { msg.textContent = "✘ 报告加载失败: " + e.message + "（run 不存在或产物缺失）"; msg.style.color = "var(--red)"; }
      frame.style.display = "none";
      el("repOpen").href = "#";
    });
    frame.src = url + "?t=" + Date.now();
  }
};

/* ==================== 数据下载页（7.6 两步, P2-2 覆盖语义） ==================== */
window.mtzData = {
  async fetchData(confirm) {
    const msg = document.getElementById("dlMsg");
    const codes = document.getElementById("dlCodes").value.split(",").map(s=>s.trim()).filter(Boolean);
    const start = document.getElementById("dlStart").value;
    const end = document.getElementById("dlEnd").value;
    if (!codes.length || !start || !end) { msg.textContent = "请填写代码与起止日期"; return; }
    msg.textContent = confirm ? "下载中…（区间大时耗时较长, 请勿关闭页面）" : "检查覆盖…";
    try {
      const rows = await API.post("/api/fetch", { codes, start, end, confirm });
      const tb = document.querySelector("#dlTable tbody");
      tb.innerHTML = rows.map(r => {
        const cov = r.covered_count
          ? `${r.covered_count} 天（${esc(r.covered_start || "—")} ~ ${esc(r.covered_end || "—")}）`
          : "无";
        let will = "—";
        if (r.status === "dry_run") will = r.missing_days > 0 ? `约 ${r.missing_days} 个交易日` : "无需下载";
        else if (r.status === "ok") will = `新增 ${r.added_rows} 行`;
        else if (r.status === "skipped") will = "无需下载";
        const segs = (r.missing_segments || []).join("、") || (r.status === "failed" ? esc(r.reason) : "—");
        return `<tr>
          <td>${esc(r.code)}</td><td>${statusBadge(r.status)}</td>
          <td class="num">${cov}</td><td class="num">${will}</td>
          <td class="muted">${segs}</td></tr>`;
      }).join("");
      msg.textContent = confirm ? "✔ 下载完成（缺失区间已补齐）" : "✔ 覆盖检查完成（无误后点「确认下载」联网补齐）";
    } catch (e) { msg.textContent = "✘ " + e.message; toast("数据操作失败: " + e.message, false); }
  }
};

/* ==================== 参数扫描进度（M4-Z2） ==================== */
window.mtzScan = {
  async load() {
    const msg = document.getElementById("scanMsg");
    const idle = document.getElementById("scanIdle");
    try {
      const j = await API.get("/api/queue");
      msg.textContent = `完成 ${j.done} / ${j.total} · ${j.total ? Math.round(j.done/j.total*100) : 0}%`;
      if (idle) idle.style.display = (j.total === 0) ? "block" : "none";
      const tb = document.querySelector("#scanTable tbody");
      tb.innerHTML = (j.results||[]).map(r => `<tr>
        <td>${esc(JSON.stringify(r.params))}</td>
        <td>${esc((r.run_id||"").slice(0,16))}</td>
        <td class="num">${r.sharpe==null?"—":Number(r.sharpe).toFixed(4)}</td>
        <td>${statusBadge(r.status)}</td></tr>`).join("");
    } catch (e) {
      const tb = document.querySelector("#scanTable tbody");
      tb.innerHTML = `<tr><td colspan="4">✘ ${esc(e.message)}</td></tr>`;
      if (idle) idle.style.display = "block";
    }
  }
};

document.addEventListener("DOMContentLoaded", () => {
  const b1 = document.getElementById("btnLoadTask");
  const b2 = document.getElementById("btnSubmit");
  const b3 = document.getElementById("btnRefreshRuns");
  const b4 = document.getElementById("btnCompare");
  const b5 = document.getElementById("btnCoverage");
  const b6 = document.getElementById("btnDownload");
  const b7 = document.getElementById("btnRefreshScan");
  if (b1) b1.onclick = () => mtzNew.loadTask();
  if (b2) b2.onclick = () => mtzNew.submit();
  if (b3) b3.onclick = () => mtzHistory.load();
  if (b4) b4.onclick = () => mtzHistory.compare();
  if (b5) b5.onclick = () => mtzData.fetchData(false);
  if (b6) b6.onclick = () => mtzData.fetchData(true);
  if (b7) b7.onclick = () => mtzScan.load();

  // 历史页: 表头排序（点击切换方向; 文本列默认升序, 指标/时间默认降序）
  document.querySelectorAll("#runsTable th[data-s]").forEach(th => {
    th.onclick = () => {
      const key = th.dataset.s, cur = window.mtzHistory._sort;
      const textCol = ["task_name", "platform", "status"].includes(key);
      window.mtzHistory._sort = { key, dir: cur.key === key ? -cur.dir : (textCol ? 1 : -1) };
      window.mtzHistory.render();
    };
  });
  // 历史页: 状态筛选 Tab + 任务名搜索
  document.querySelectorAll("#histTabs .tab").forEach(t => {
    t.onclick = () => {
      document.querySelectorAll("#histTabs .tab").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      window.mtzHistory._filter = t.dataset.f;
      window.mtzHistory.render();
    };
  });
  const hs = document.getElementById("histSearch");
  if (hs) hs.oninput = () => window.mtzHistory.render();

  // 通用弹窗: 关闭按钮 + 点遮罩关闭
  const mc = document.getElementById("modalClose");
  if (mc) mc.onclick = closeModal;
  const mm = document.getElementById("modalMask");
  if (mm) mm.onclick = closeModal;

  window.mtzNew.init();
});
