/* mtzQuant Web 页面逻辑（M4-X, 原生 JS 无构建链, 9.1: 数据一律来自 REST/DB 同源） */
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
  }
};

const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));

/* ==================== 新建回测页 ==================== */
window.mtzNew = {
  async loadTask() {
    const path = document.getElementById("newTask").value.trim();
    const msg = document.getElementById("newMsg");
    if (!path) { msg.textContent = "请输入任务 JSON 路径"; return; }
    const r = await fetch(`/api/read-task?path=${encodeURIComponent(path)}`).catch(()=>null);
    if (r && r.ok) {
      const j = await r.json();
      document.getElementById("newTaskJson").value = JSON.stringify(j, null, 2);
      msg.textContent = "已读取: " + path;
    } else {
      // 无读取端点时, 提示直接在文本域粘贴
      msg.textContent = "读取失败——请直接在下方文本域粘贴任务 JSON（或本地 mtzquant validate）";
    }
  },
  async submit() {
    const msg = document.getElementById("newMsg");
    let task;
    try { task = JSON.parse(document.getElementById("newTaskJson").value); }
    catch (e) { msg.textContent = "任务 JSON 解析失败: " + e.message; return; }
    msg.textContent = "提交中…";
    try {
      const j = await API.post("/api/backtests", task);
      msg.textContent = "✔ 已提交: " + j.run_id;
      location.hash = "#/monitor?run=" + j.run_id;
    } catch (e) { msg.textContent = "✘ " + e.message; }
  }
};

/* ==================== 历史 / 对比页 ==================== */
window.mtzHistory = {
  _selected: new Set(),
  async load() {
    const tb = document.querySelector("#runsTable tbody");
    try {
      const runs = await API.get("/api/runs");
      tb.innerHTML = runs.map(r => `<tr>
        <td><input type="checkbox" class="run-check" data-id="${esc(r.run_id)}" ${this._selected.has(r.run_id)?"checked":""}></td>
        <td>${esc((r.run_id||"").slice(0,20))}</td>
        <td>${esc(r.task_name)}</td>
        <td>${esc(r.status)}</td>
        <td class="num">${r.sharpe==null?"—":Number(r.sharpe).toFixed(3)}</td>
        <td>${esc((r.started_at||"").slice(0,19))}</td>
        <td><a href="#/report?run=${encodeURIComponent(r.run_id)}">报告</a></td>
      </tr>`).join("");
      tb.querySelectorAll(".run-check").forEach(cb => cb.onchange = () => {
        if (cb.checked) this._selected.add(cb.dataset.id);
        else this._selected.delete(cb.dataset.id);
        while (this._selected.size > 6) this._selected.delete([...this._selected][0]);
      });
    } catch (e) { tb.innerHTML = `<tr><td colspan="7">✘ ${esc(e.message)}</td></tr>`; }
  },
  async compare() {
    const ids = [...this._selected];
    if (!ids.length) return;
    const box = document.getElementById("compareBox");
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

async function drawCompareNav(root, others) {
  const navs = await Promise.all([root, ...others.split(",").filter(Boolean)].map(async rid => {
    try { return { rid, rows: await API.get(`/api/runs/${rid}/navs`) }; } catch (e) { return { rid, rows: [] }; }
  }));
  const el = document.getElementById("compareChart");
  const chart = echarts.init(el);
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

/* ==================== 数据下载页（7.6 两步） ==================== */
window.mtzData = {
  async fetchData(confirm) {
    const msg = document.getElementById("dlMsg");
    const codes = document.getElementById("dlCodes").value.split(",").map(s=>s.trim()).filter(Boolean);
    const start = document.getElementById("dlStart").value;
    const end = document.getElementById("dlEnd").value;
    if (!codes.length || !start || !end) { msg.textContent = "需要代码与区间"; return; }
    msg.textContent = confirm ? "下载中…" : "检查覆盖…";
    try {
      const rows = await API.post("/api/fetch", { codes, start, end, confirm });
      const tb = document.querySelector("#dlTable tbody");
      tb.innerHTML = rows.map(r => `<tr>
        <td>${esc(r.code)}</td><td>${esc(r.status)}</td>
        <td class="num">${r.added_rows}</td>
        <td>${esc((r.range||[]).join(" ~ "))}</td>
        <td>${esc(r.reason)}</td></tr>`).join("");
      msg.textContent = confirm ? "✔ 下载完成" : "✔ 覆盖检查完成（确认后下载）";
    } catch (e) { msg.textContent = "✘ " + e.message; }
  }
};

/* ==================== 参数扫描进度（M4-Z2） ==================== */
window.mtzScan = {
  async load() {
    const msg = document.getElementById("scanMsg");
    try {
      const j = await API.get("/api/queue");
      msg.textContent = `完成 ${j.done} / ${j.total} · ${j.total ? Math.round(j.done/j.total*100) : 0}%`;
      const tb = document.querySelector("#scanTable tbody");
      tb.innerHTML = (j.results||[]).map(r => `<tr>
        <td>${esc(JSON.stringify(r.params))}</td>
        <td>${esc((r.run_id||"").slice(0,16))}</td>
        <td class="num">${r.sharpe==null?"—":Number(r.sharpe).toFixed(4)}</td>
        <td>${esc(r.status)}</td></tr>`).join("");
    } catch (e) {
      const tb = document.querySelector("#scanTable tbody");
      tb.innerHTML = `<tr><td colspan="4">✘ ${esc(e.message)}</td></tr>`;
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
});
