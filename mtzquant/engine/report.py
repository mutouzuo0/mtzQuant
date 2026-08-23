# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/23 12:30:00
# @description : J mtzQuant report.html：自包含报告（指标卡/交互净值图/成交分页, 9.2）

"""report.html（设计 9.2）——自包含单文件回测报告。

内容:
  指标卡        gross/net 双口径（8.4, Metrics.compute 各算一组; 页面标注两口径差异=累计费用）
  语义保真声明区 completed_exact | completed_degraded + 降级清单（4.9.2 纪律）
  净值+回撤     内嵌 canvas + JS（左轴净值/基准 · 右轴资产总值¥ · 下带回撤;
                悬浮十字线看当日数值, ↑/↓ 以鼠标处为锚缩放, ←/→ 或左键拖动平移;
                无 CDN, 离线可开）
  成交明细表    滚动容器（600px 高/表头吸顶）+ 分页每页 100 条（上一页/下一页 +
                页码直达前 10 页; 无 order_id 列）+ 全量下载链接（orders.csv）
  指标口径附注  8.4 公式 + metrics_version

数据源: results/<run_id>/{summary.json, daily_stats.csv, orders.csv}（与 DB 同源投影, 9.1）。
"""

from __future__ import annotations

import ast
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.metrics import Metrics

_METRICS_VERSION = "8.4-v1"


def render_report(
    run_id: str,
    *,
    out_root: Path | str = "results",
    out_path: Path | str | None = None,
    friction_path: Path | str | None = None,
) -> Path:
    """生成自包含 report.html; 返回输出路径。

    friction_path（M3-V3, D5）: 可选并入双引擎摩擦归因报告（friction_report.json）。
    """
    run_dir = Path(out_root) / run_id
    if not run_dir.is_dir():
        raise MtzQuantError(
            f"报告源目录不存在: {run_dir}",
            stage="report",
            hint="先 `mtzquant run` 生成 results/<run_id>；或检查 --out 路径",
        )
    summary = _load_json(run_dir / "summary.json") or {}
    navs = _load_navs(run_dir)
    orders = _load_orders(run_dir)
    fills = _load_fills(run_dir)
    events = _load_events(run_dir)
    task = _load_json(run_dir / "task.json") or {}
    friction = _load_json(Path(friction_path)) if friction_path else None

    html_text = _render_html(run_id, summary, navs, orders, fills, events, task, friction)
    out = Path(out_path) if out_path else run_dir / "report.html"
    out.write_text(html_text, encoding="utf-8")
    return out


# ------------------------------------------------------------------
# 数据读取
# ------------------------------------------------------------------
def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_navs(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "daily_stats.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"trade_date": str})
    return df.to_dict("records")


def _load_orders(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "orders.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"order_id": str})
    return df.to_dict("records")


def _load_fills(run_dir: Path) -> list[dict[str, Any]]:
    """成交（含容量证据列 bar_volume/participation_rate, 8.4.4）。"""
    path = run_dir / "fills.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"order_id": str})
    return df.to_dict("records")


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "order_events.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"order_id": str})
    return df.to_dict("records")


def _run_exec_time(run_id: str, summary: dict[str, Any]) -> str:
    """回测执行时间: run_id 毫秒时间戳 → Asia/Shanghai 本地时间（含时分秒）。

    run_id 形如 r_<毫秒>_<hash8>（8.8 确定性）; 无法解析时退回 summary.exported_at。
    """
    m = re.match(r"^r_(\d{13})_", run_id)
    if m:
        dt = datetime.fromtimestamp(int(m.group(1)) / 1000, tz=ZoneInfo("Asia/Shanghai"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    exported = str((summary or {}).get("exported_at") or "")
    return exported[:19].replace("T", " ")


def _parse_info_json(raw: Any) -> dict[str, Any]:
    """容错解析 info_json（CSV 可能落 dict-repr / JSON 字符串 / 空）。"""
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return {}
    for loader in (json.loads, ast.literal_eval):
        try:
            val = loader(text)
            return val if isinstance(val, dict) else {}
        except (ValueError, SyntaxError):
            continue
    return {}


# ------------------------------------------------------------------
# 容量证据（8.4.4, M2-P4）: 参与率分布/容量截断/不可成交/成交延迟
# ------------------------------------------------------------------
def _capacity_stats(
    fills: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    rates = [float(f.get("participation_rate") or 0.0) for f in fills if f.get("volume")]
    part = {}
    if rates:
        arr = np.asarray(rates, dtype=float)
        part = {
            "max": float(arr.max()),
            "mean": float(arr.mean()),
            "p95": float(np.percentile(arr, 95)),
            "count": int(len(arr)),
        }
    else:
        part = {"max": 0.0, "mean": 0.0, "p95": 0.0, "count": 0}

    # 容量截断（order_events info_json.capacity_capped）
    capped = sum(1 for e in events if _parse_info_json(e.get("info_json")).get("capacity_capped"))

    # 不可成交（orders 终态）; 一字板/停牌拆分（expire 事件 info_json.one_word_limit）
    total = max(len(orders), 1)
    statuses = [str(o.get("status", "")) for o in orders]
    expired = sum(1 for s in statuses if s == "EXPIRED")
    rejected = sum(1 for s in statuses if s == "REJECTED")
    one_word = sum(
        1
        for e in events
        if e.get("event_type") == "expire"
        and _parse_info_json(e.get("info_json")).get("one_word_limit")
    )
    suspend_expire = max(expired - one_word, 0)

    # 成交延迟（fill_time − 订单 submitted_at, 按 order_id join）
    sub = {str(o.get("order_id")): o.get("submitted_at") for o in orders}
    lat_days: list[float] = []
    for f in fills:
        ft, st = f.get("fill_time"), sub.get(str(f.get("order_id")))
        if ft and st:
            try:
                lat_days.append((pd.Timestamp(ft) - pd.Timestamp(st)).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                continue
    lat = {}
    if lat_days:
        arr = np.asarray(lat_days, dtype=float)
        lat = {
            "min": float(arr.min()),
            "mean": float(arr.mean()),
            "p95": float(np.percentile(arr, 95)),
            "max": float(arr.max()),
            "count": int(len(arr)),
        }
    return {
        "participation": part,
        "truncated": capped,
        "truncation_ratio": round(capped / total, 6),
        "expired": expired,
        "rejected": rejected,
        "one_word": one_word,
        "suspend_expire": suspend_expire,
        "unfillable_ratio": round((expired + rejected) / total, 6),
        "latency_days": lat,
        "total_orders": len(orders),
    }


def _render_capacity(stats: dict[str, Any]) -> str:
    """8.4.4 容量与摩擦证据四表+图（内嵌, 无 CDN）。"""
    part = stats["participation"]
    lat = stats["latency_days"]
    rows_p = (
        f"<tr><td>样本</td><td>{part['count']}</td></tr>"
        f"<tr><td>max</td><td>{part['max']:.4f}</td></tr>"
        f"<tr><td>mean</td><td>{part['mean']:.4f}</td></tr>"
        f"<tr><td>p95</td><td>{part['p95']:.4f}</td></tr>"
        if part.get("count")
        else "<tr><td colspan='2' class='dim'>无成交</td></tr>"
    )
    tot = stats["total_orders"]
    rows_u = (
        f"<tr><td>容量截断单数</td><td>{stats['truncated']} "
        f"（占比 {stats['truncation_ratio']:.2%}）</td></tr>"
        f"<tr><td>过期(expired)</td><td>{stats['expired']}</td></tr>"
        f"<tr><td>· 一字板</td><td>{stats['one_word']}</td></tr>"
        f"<tr><td>· 停牌/当日未成交</td><td>{stats['suspend_expire']}</td></tr>"
        f"<tr><td>拒单(rejected)</td><td>{stats['rejected']}</td></tr>"
        f"<tr><td>不可成交合计</td><td>{stats['expired'] + stats['rejected']} "
        f"（占比 {stats['unfillable_ratio']:.2%}）</td></tr>"
        f"<tr><td>订单总数</td><td>{tot}</td></tr>"
    )
    rows_l = (
        f"<tr><td>样本</td><td>{lat['count']}</td></tr>"
        f"<tr><td>min</td><td>{lat['min']:.3f} 日</td></tr>"
        f"<tr><td>mean</td><td>{lat['mean']:.3f} 日</td></tr>"
        f"<tr><td>p95</td><td>{lat['p95']:.3f} 日</td></tr>"
        f"<tr><td>max</td><td>{lat['max']:.3f} 日</td></tr>"
        if lat.get("count")
        else "<tr><td colspan='2' class='dim'>无成交</td></tr>"
    )
    # 参与率分布迷你条形（0~5% 分段, 占整体宽度）
    bars = _participation_bars(stats["participation"])
    return f"""
<h2>容量与摩擦证据（8.4.4）</h2>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">
  <div><h3 style="font-size:14px">参与率分布（volume / bar_volume）</h3>
    <table><thead><tr><th>统计</th><th>值</th></tr></thead><tbody>{rows_p}</tbody></table>
    <div style="margin-top:6px">{bars}</div></div>
  <div><h3 style="font-size:14px">不可成交 / 容量截断</h3>
    <table><thead><tr><th>项</th><th>单数</th></tr></thead><tbody>{rows_u}</tbody></table></div>
  <div><h3 style="font-size:14px">成交延迟分布（挂单→成交, 日）</h3>
    <table><thead><tr><th>统计</th><th>值</th></tr></thead><tbody>{rows_l}</tbody></table></div>
</div>
"""


def _participation_bars(part: dict[str, Any]) -> str:
    """参与率 0~50% 分 5 档迷你条形（占 bar_volume 比例的成交样本）。"""
    # 用 mean/p95/max 三点示意档位（简化无直方源数据）
    tips = [
        ("mean", part.get("mean", 0.0)),
        ("p95", part.get("p95", 0.0)),
        ("max", part.get("max", 0.0)),
    ]

    def _bar(label: str, v: float) -> str:
        width = min(100.0, v / 0.5 * 100.0) if v else 0.0
        return (
            f'<div class="dim" style="margin:2px 0">{label}: '
            f'<span style="display:inline-block;width:{width:.0f}%;height:10px;'
            f'background:#2563eb;vertical-align:middle"></span> {v:.4f}</div>'
        )

    return "".join(_bar(k, v) for k, v in tips)


# ------------------------------------------------------------------
# 摩擦归因报告并入 report.html（M3-V3, D5: friction_report 同源 JSON）
# ------------------------------------------------------------------
def _friction_hash(friction: dict[str, Any]) -> str:
    """摩擦报告数据版本锚点（12 位缩写; 缺失 → —）。"""
    h = str(friction.get("data_manifest_hash") or "").strip()
    return html.escape(h[:12]) if h else "—"


def _render_friction(friction: dict[str, Any]) -> str:
    """双引擎摩擦归因章节（5.8/8.4.4）——并入 report.html 新章节。"""
    totals = friction.get("totals", {})
    meta = friction.get("meta", {})
    periods = friction.get("periods", [])
    rows = ""
    for p in periods[:100]:
        rows += (
            f"<tr><td>{html.escape(str(p.get('period', '')))}</td>"
            f"<td>{p.get('vector_ret', 0):.4f}</td>"
            f"<td>{p.get('event_ret', 0):.4f}</td>"
            f"<td style='color:{'#dc2626' if p.get('diff', 0) < 0 else '#16a34a'}'>"
            f"{p.get('diff', 0):.4f}</td>"
            f"<td>{p.get('fee_ret', 0):.6f}</td>"
            f"<td>{p.get('slippage_ret', 0):.6f}</td>"
            f"<td>{p.get('capacity_ret', 0):.6f}</td>"
            f"<td>{p.get('turnover', 0):.4f}</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='8' class='dim'>无调仓期（买入持有）</td></tr>"
    return f"""
<h2>摩擦归因报告（双引擎, 8.4.4 / D5）</h2>
<p class="dim">向量化引擎（快, 目标权重级）vs 事件驱动引擎（真撮合）——逐期收益差 = 摩擦成本;
数据版本锚点: <code>{_friction_hash(friction)}</code> ·
事件状态: {html.escape(str(meta.get("event_status", "")))}
</p>
<table>
<thead><tr><th>调仓日</th><th>向量收益</th><th>事件收益</th><th>差值</th>
<th>费用</th><th>滑点</th><th>容量/T+1</th><th>换手</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="dim">合计: 总收益差 {totals.get("total_return_diff", 0):.6f} ·
费用 {totals.get("fee_cost", 0):.6f} ·
滑点 {totals.get("slippage_cost", 0):.6f} ·
容量/T+1 {totals.get("capacity_cost", 0):.6f} ·
总换手 {totals.get("total_turnover", 0):.6f}</p>
"""


# ------------------------------------------------------------------
# 指标（gross/net 双口径, 8.4）
# ------------------------------------------------------------------
def _metric_card(navs: list[dict[str, Any]], *, key: str) -> dict[str, str]:
    """按净值列（nav=net / gross_nav=gross）计算 8.4 指标卡。"""
    series = [float(r[key]) for r in navs if r.get(key) is not None]
    if len(series) < 2:
        return {}
    m = Metrics.compute(np.asarray(series, dtype=float), dates=None)
    risk = m.risk
    return {
        "总收益": f"{risk.total_return:.2%}",
        "年化收益": f"{risk.annual_return:.2%}",
        "年化波动": f"{risk.annual_volatility:.2%}",
        "夏普": f"{risk.sharpe:.3f}",
        "索提诺": f"{risk.sortino:.3f}",
        "卡玛": f"{risk.calmar:.3f}",
        "最大回撤": f"{risk.max_drawdown.value:.2%}",
        "日胜率": f"{risk.daily_win_rate:.2%}",
    }


# ------------------------------------------------------------------
# 交互图（净值 + 回撤 + 资产总值, canvas + 内嵌 JS, 无 CDN）
# ------------------------------------------------------------------
def _nav_chart_payload(navs: list[dict[str, Any]]) -> dict[str, Any]:
    """图表数据载荷（净值/基准/资产总值/回撤 × trade_date; null = 缺失）。"""
    dates: list[str] = []
    net: list[float | None] = []
    bench: list[float | None] = []
    total: list[float | None] = []
    dd: list[float | None] = []
    for r in navs:
        if r.get("nav") is None:
            continue
        dates.append(str(r.get("trade_date", ""))[:10])
        net.append(round(float(r["nav"]), 6))
        bench.append(
            round(float(r["benchmark_nav"]), 6) if r.get("benchmark_nav") is not None else None
        )
        total.append(
            round(float(r["total_value"]), 2) if r.get("total_value") is not None else None
        )
        dd.append(round(float(r["drawdown"]), 6) if r.get("drawdown") is not None else 0.0)
    return {"dates": dates, "nav": net, "bench": bench, "total": total, "dd": dd}


def _render_nav_chart_html(navs: list[dict[str, Any]]) -> str:
    """净值+回撤交互图 HTML（canvas; 悬浮数值 / ↑↓ 缩放 / ←→·左键拖动平移）。"""
    payload = _nav_chart_payload(navs)
    if len(payload["dates"]) < 2:
        return '<p class="muted">净值点不足, 无法绘图</p>'
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (
        '<div class="chart-wrap" id="nav-chart-wrap">'
        '<canvas id="nav-chart" width="760" height="400"></canvas>'
        '<div id="nav-tip" class="tip"></div></div>'
        '<p class="dim">左轴: 净值/基准 · 右轴: 资产总值(¥) · 下带: 回撤;'
        " 悬浮查看数值; ↑/↓ 以鼠标处为锚缩放时间轴; ←/→ 或按住左键拖动平移</p>"
        "<script>\nvar NAV_DATA = " + data_json + ";\n" + _NAV_CHART_JS + "</script>"
    )


# 交互图引擎（纯前端 canvas: 双轴折线 + 回撤带 + 十字线 tooltip + 键盘缩放平移）
_NAV_CHART_JS = """(function () {
  var D = NAV_DATA, n = D.dates.length;
  var i0 = 0, i1 = n - 1, hoverIdx = -1;
  var cv = document.getElementById('nav-chart');
  var tip = document.getElementById('nav-tip');
  var ctx = cv.getContext('2d');
  var W = 760, H = 400, PL = 46, PR = 62, PT = 26, MID = 226, DDT = 248, DB = H - 28;
  var dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  cv.style.width = W + 'px'; cv.style.height = H + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  var sc = null;

  function fmtMoney(v) {
    if (Math.abs(v) >= 10000) return (v / 10000).toFixed(1) + '万';
    return v.toFixed(0);
  }
  function xAt(i) { return PL + (i - i0) * (W - PL - PR) / Math.max(1, i1 - i0); }
  function computeScales() {
    var nl = 1e18, nh = -1e18, tl = 1e18, th = -1e18, md = 0;
    for (var i = i0; i <= i1; i++) {
      nl = Math.min(nl, D.nav[i]); nh = Math.max(nh, D.nav[i]);
      if (D.bench[i] != null) { nl = Math.min(nl, D.bench[i]); nh = Math.max(nh, D.bench[i]); }
      if (D.total[i] != null) { tl = Math.min(tl, D.total[i]); th = Math.max(th, D.total[i]); }
      md = Math.max(md, Math.abs(D.dd[i]));
    }
    var sp = (nh - nl) || 1; nl -= sp * 0.06; nh += sp * 0.06;
    var ts = (th - tl) || 1; tl -= ts * 0.06; th += ts * 0.06;
    sc = { nl: nl, nh: nh, tl: tl, th: th, md: md || 0.0001 };
  }
  function yN(v) { return MID - (v - sc.nl) / (sc.nh - sc.nl) * (MID - PT); }
  function yT(v) { return MID - (v - sc.tl) / (sc.th - sc.tl) * (MID - PT); }
  function yD(v) { return DDT + Math.abs(v) / sc.md * (DB - DDT); }
  function strokePts(pts) {
    ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
    for (var k = 1; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
    ctx.stroke();
  }

  function draw() {
    computeScales();
    ctx.clearRect(0, 0, W, H);
    var leg = [['#2563eb', '净值'], ['#f59e0b', '基准'],
               ['#16a34a', '资产总值(右轴)'], ['#f43f5e', '回撤(下带)']];
    var lx = PL;
    ctx.font = '11px sans-serif';
    for (var k = 0; k < leg.length; k++) {
      ctx.fillStyle = leg[k][0]; ctx.fillRect(lx, 2, 10, 3);
      ctx.fillStyle = '#374151'; ctx.fillText(leg[k][1], lx + 14, 9);
      lx += 14 + ctx.measureText(leg[k][1]).width + 24;
    }
    ctx.font = '10px sans-serif';
    for (var g = 0; g < 5; g++) {
      var v = sc.nl + (sc.nh - sc.nl) * g / 4, gy = yN(v);
      ctx.strokeStyle = '#f3f4f6'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(PL, gy); ctx.lineTo(W - PR, gy); ctx.stroke();
      ctx.fillStyle = '#6b7280'; ctx.textAlign = 'right';
      ctx.fillText(v.toFixed(2), PL - 4, gy + 3);
    }
    ctx.fillStyle = '#16a34a'; ctx.textAlign = 'left';
    for (var g2 = 0; g2 < 5; g2++) {
      var tv = sc.tl + (sc.th - sc.tl) * g2 / 4, ty = yT(tv);
      ctx.fillText(fmtMoney(tv), W - PR + 6, ty + 3);
    }
    ctx.strokeStyle = '#d1d5db'; ctx.beginPath();
    ctx.moveTo(PL, DDT); ctx.lineTo(W - PR, DDT); ctx.stroke();
    ctx.fillStyle = '#6b7280'; ctx.textAlign = 'right';
    ctx.fillText('0', PL - 4, DDT + 3);
    ctx.fillText('-' + (sc.md * 100).toFixed(1) + '%', PL - 4, DB + 3);
    var np = [], bp = [], tp = [], dp = [];
    for (var i = i0; i <= i1; i++) {
      np.push([xAt(i), yN(D.nav[i])]);
      if (D.bench[i] != null) bp.push([xAt(i), yN(D.bench[i])]);
      if (D.total[i] != null) tp.push([xAt(i), yT(D.total[i])]);
      dp.push([xAt(i), yD(D.dd[i])]);
    }
    ctx.beginPath(); ctx.moveTo(xAt(i0), DDT);
    for (var q = 0; q < dp.length; q++) ctx.lineTo(dp[q][0], dp[q][1]);
    ctx.lineTo(xAt(i1), DDT); ctx.closePath();
    ctx.fillStyle = 'rgba(244,63,94,0.15)'; ctx.fill();
    ctx.strokeStyle = '#f43f5e'; ctx.lineWidth = 1.2; strokePts(dp);
    ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.4; strokePts(bp);
    ctx.strokeStyle = '#2563eb'; ctx.lineWidth = 1.8; strokePts(np);
    ctx.strokeStyle = '#16a34a'; ctx.lineWidth = 1.4;
    ctx.setLineDash([3, 2]); strokePts(tp); ctx.setLineDash([]);
    var step = Math.max(1, Math.round((i1 - i0) / 5));
    ctx.fillStyle = '#6b7280'; ctx.textAlign = 'center';
    for (var t = i0; t <= i1; t += step) ctx.fillText(D.dates[t], xAt(t), H - 8);
    if (hoverIdx >= i0 && hoverIdx <= i1) {
      var hx = xAt(hoverIdx);
      ctx.strokeStyle = '#9ca3af'; ctx.setLineDash([2, 2]);
      ctx.beginPath(); ctx.moveTo(hx, PT); ctx.lineTo(hx, DB); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#2563eb'; ctx.beginPath();
      ctx.arc(hx, yN(D.nav[hoverIdx]), 3, 0, 6.284); ctx.fill();
    }
  }

  function showTip(i, mx) {
    var b = D.bench[i], t = D.total[i];
    var money = t == null ? '—' : '¥' + t.toLocaleString('zh-CN', {maximumFractionDigits: 2});
    var rows = [
      ['净值', D.nav[i].toFixed(4), '#2563eb'],
      ['基准', b == null ? '—' : b.toFixed(4), '#f59e0b'],
      ['资产总值', money, '#16a34a'],
      ['回撤', '-' + (D.dd[i] * 100).toFixed(2) + '%', '#f43f5e']
    ];
    var h = '<div class="tip-title">' + D.dates[i] + '</div>';
    for (var k = 0; k < rows.length; k++) {
      h += '<div>' + rows[k][0] + ' <b style="color:' + rows[k][2] + '">' +
           rows[k][1] + '</b></div>';
    }
    tip.innerHTML = h; tip.style.display = 'block';
    var left = mx + 14;
    if (left + tip.offsetWidth > W - 4) left = mx - tip.offsetWidth - 14;
    tip.style.left = Math.max(0, left) + 'px'; tip.style.top = '10px';
  }

  var dragging = false, dragX0 = 0, dragI0 = 0, dragW = 0;
  cv.addEventListener('mousedown', function (e) {
    dragging = true; dragX0 = e.offsetX; dragI0 = i0; dragW = i1 - i0;
    cv.style.cursor = 'grabbing';
    tip.style.display = 'none';
    e.preventDefault();
  });
  cv.addEventListener('mousemove', function (e) {
    var mx = e.offsetX;
    if (dragging) {
      var di = Math.round((mx - dragX0) / (W - PL - PR) * dragW);
      i0 = Math.max(0, Math.min(n - 1 - dragW, dragI0 - di));
      i1 = i0 + dragW;
      hoverIdx = -1;
      draw();
      return;
    }
    var i = i0 + Math.round((mx - PL) / (W - PL - PR) * (i1 - i0));
    hoverIdx = Math.max(i0, Math.min(i1, i));
    draw(); showTip(hoverIdx, mx);
  });
  cv.addEventListener('mouseup', function () {
    dragging = false; cv.style.cursor = 'crosshair';
  });
  cv.addEventListener('mouseleave', function () {
    dragging = false; cv.style.cursor = 'crosshair';
    hoverIdx = -1; tip.style.display = 'none'; draw();
  });
  function zoom(f) {
    var w = Math.round((i1 - i0 + 1) * f);
    w = Math.max(20, Math.min(n, w));
    var c = (hoverIdx >= i0 && hoverIdx <= i1) ? hoverIdx : Math.round((i0 + i1) / 2);
    i0 = Math.max(0, c - Math.floor(w / 2));
    i1 = Math.min(n - 1, i0 + w - 1);
    i0 = Math.max(0, i1 - w + 1);
    draw();
  }
  function pan(s) {
    var step = Math.max(1, Math.round((i1 - i0 + 1) * 0.1)) * s;
    i0 += step; i1 += step;
    if (i0 < 0) { i1 -= i0; i0 = 0; }
    if (i1 > n - 1) { i0 -= i1 - (n - 1); i1 = n - 1; }
    i0 = Math.max(0, i0);
    draw();
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowUp') { zoom(0.8); e.preventDefault(); }
    else if (e.key === 'ArrowDown') { zoom(1.25); e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { pan(-1); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { pan(1); e.preventDefault(); }
  });
  draw();
})();
"""


# ------------------------------------------------------------------
# HTML 渲染
# ------------------------------------------------------------------
def _render_html(
    run_id: str,
    summary: dict[str, Any],
    navs: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    events: list[dict[str, Any]],
    task: dict[str, Any],
    friction: dict[str, Any] | None = None,
) -> str:
    status = summary.get("status", "?")
    degradations = summary.get("degradations", []) or []
    metrics = summary.get("metrics", {}) or {}
    net_card = _metric_card(navs, key="nav")
    gross_card = _metric_card(navs, key="gross_nav")
    capacity = _render_capacity(_capacity_stats(fills, orders, events))
    fees = summary.get("fees", {}) or {}
    fee_total = sum(float(v) for v in fees.values())

    def _cards(card: dict[str, str]) -> str:
        return "".join(
            f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
            for k, v in card.items()
        )

    # 语义保真（N1/N2: 降级=语义事件; 摩擦=cash_capped 缩量, 不影响完成状态）
    fidelity = (
        '<span class="badge ok">completed_exact</span>'
        if status == "completed_exact"
        else '<span class="badge warn">completed_degraded</span>'
    )
    frictions = summary.get("frictions", []) or []
    deg_items = (
        "".join(f"<li>{html.escape(str(d))}</li>" for d in degradations[:50]) or "<li>无降级</li>"
    )
    friction_items = "".join(f"<li>{html.escape(str(f))}</li>" for f in frictions[:200])
    friction_list_section = (
        (
            '<details><summary style="cursor:pointer">撮合摩擦明细（'
            f"{len(frictions)} 笔缩量部分成交, 默认折叠——不影响完成状态）</summary>"
            f'<ul style="max-height:360px;overflow-y:auto">{friction_items}</ul></details>'
        )
        if frictions
        else ""
    )

    # 成交表（分页, 每页 100 条; 无 order_id 列, 倒序=最新在前）
    orders_payload = [
        {
            "code": str(o.get("code", "")),
            "side": str(o.get("side", "")),
            "qty": str(o.get("qty", "")),
            "status": str(o.get("status", "")),
            "at": str(o.get("submitted_at", "")),
        }
        for o in reversed(orders)
    ]
    if orders_payload:
        ojson = json.dumps(orders_payload, ensure_ascii=False, separators=(",", ":")).replace(
            "</", "<\\/"
        )
        orders_section = (
            '<div class="table-scroll"><table><thead><tr><th>代码</th><th>方向</th>'
            "<th>数量</th><th>状态</th><th>提交时间</th></tr></thead>"
            '<tbody id="orders-body"></tbody></table></div>'
            '<div class="pager"><button id="orders-prev">上一页</button>'
            '<span id="orders-pages"></span>'
            '<span id="orders-page"></span>'
            '<button id="orders-next">下一页</button></div>'
            '<p class="dim"><a href="orders.csv">下载全量订单 (orders.csv)</a></p>'
            "<script>\nvar ORDERS_DATA = " + ojson + ";\n" + _ORDERS_PAGER_JS + "</script>"
        )
    else:
        orders_section = '<p class="dim">无订单</p>'

    max_dd = metrics.get("max_drawdown", {}) or {}
    peak, trough = max_dd.get("peak_date"), max_dd.get("trough_date")

    # 标题标识: 回测执行时间（run_id 毫秒时间戳 → 本地时间; 退回 exported_at / run_id）
    title_label = _run_exec_time(run_id, summary) or run_id
    friction_section = _render_friction(friction) if friction else ""

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>mtzQuant 回测报告 · {html.escape(title_label)}</title>
<style>
  body{{font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
        max-width:960px;margin:24px auto;padding:0 16px;color:#1f2937}}
  h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;
        border-bottom:1px solid #e5e7eb;padding-bottom:6px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
  .card{{border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px}}
  .card .k{{font-size:12px;color:#6b7280}} .card .v{{font-size:18px;font-weight:600;margin-top:2px}}
  .badge{{padding:2px 10px;border-radius:999px;font-size:13px}}
  .badge.ok{{background:#dcfce7;color:#166534}} .badge.warn{{background:#fef3c7;color:#92400e}}
  table{{border-collapse:collapse;width:100%;font-size:13px}}
  th,td{{border:1px solid #e5e7eb;padding:5px 8px;text-align:left;white-space:nowrap}}
  th{{background:#f9fafb}} .dim{{color:#6b7280;font-size:13px}}
  .muted{{color:#6b7280;font-size:13px}}
  .chart-wrap{{position:relative;width:760px;max-width:100%}}
  #nav-chart{{border:1px solid #f3f4f6;border-radius:6px;cursor:crosshair}}
  .tip{{display:none;position:absolute;top:10px;left:0;background:#fff;
        border:1px solid #e5e7eb;border-radius:6px;padding:6px 10px;font-size:12px;
        line-height:1.7;pointer-events:none;box-shadow:0 1px 4px rgba(0,0,0,.08);
        white-space:nowrap}}
  .tip-title{{font-weight:600;margin-bottom:2px}}
  .pager{{margin-top:8px;display:flex;gap:8px;align-items:center;font-size:13px}}
  .pager button{{padding:4px 12px;border:1px solid #d1d5db;background:#fff;
        border-radius:6px;cursor:pointer}}
  .pager button:disabled{{opacity:.4;cursor:default}}
  .table-scroll{{max-height:600px;overflow-y:auto;border:1px solid #e5e7eb;
        border-radius:6px}}
  .table-scroll thead th{{position:sticky;top:0;z-index:1}}
  .pager .page-btn{{min-width:30px;padding:4px 8px;margin:0 1px}}
  .pager .page-btn.active{{background:#2563eb;color:#fff;border-color:#2563eb}}
</style></head><body>
<h1>mtzQuant 回测报告 · {html.escape(title_label)}</h1>
<p class="muted">任务: {html.escape(str(task.get("task_name", "")))} · 区间:
   {html.escape(str(task.get("backtest", {}).get("start", "")))} ~
   {html.escape(str(task.get("backtest", {}).get("end", "")))} ·
   run_id: {html.escape(run_id)} ·
   metrics_version: {html.escape(str(summary.get("metrics_version", _METRICS_VERSION)))}</p>

<h2>指标卡（gross / net 双口径）</h2>
<div class="cards"><div class="card" style="grid-column:1/-1"><div class="k">语义保真</div>
  <div class="v" style="font-size:14px">{fidelity}
  <span class="muted">最大回撤峰/谷: {peak} → {trough}</span></div></div></div>
<p class="dim">▍ 净值口径（扣费后, 基于 nav）</p>
<div class="cards">{_cards(net_card)}</div>
<p class="dim">▍ 毛利口径（未扣费, 基于 gross_nav; 与净值口径差异 = 累计费用 ¥{fee_total:.2f}）</p>
<div class="cards">{_cards(gross_card)}</div>

<h2>净值 + 回撤（悬浮查看数值 · ↑/↓ 缩放时间轴）</h2>
{_render_nav_chart_html(navs)}

<h2>语义保真声明（降级与撮合摩擦）</h2>
<p class="dim">共 {len(degradations)} 条语义降级 · {len(frictions)} 笔撮合摩擦
（缩量部分成交, 不影响完成状态）。</p>
<ul>{deg_items}</ul>
{friction_list_section}

<h2>成交明细（每页 100 条）</h2>
{orders_section}

{capacity}

{friction_section}

<h2>指标口径附注</h2>
<p class="muted">指标公式: 年化=nav^(250/n)-1（ANN=250）; 波动 ddof=1;
        索提诺 TDD=√(mean(min(r-MAR,0)²));
最大回撤含峰谷日; 夏普 rf=0; 指标版本
        {html.escape(str(summary.get("metrics_version", _METRICS_VERSION)))}。
净值/回撤与成交分页为内嵌 canvas/JS（离线可用, 无外部依赖）。</p>
</body></html>"""
    return html_doc


# 成交明细分页引擎（纯前端: 每页 100 条, 上一页/下一页 + 页码标签）
_ORDERS_PAGER_JS = """(function () {
  var O = ORDERS_DATA, PAGE = 100, page = 0;
  var pages = Math.max(1, Math.ceil(O.length / PAGE));
  var tb = document.getElementById('orders-body');
  var lbl = document.getElementById('orders-page');
  var box = document.getElementById('orders-pages');
  var prev = document.getElementById('orders-prev');
  var next = document.getElementById('orders-next');
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function renderPages() {
    var s = '', show = Math.min(pages, 10);
    for (var p = 0; p < show; p++) {
      s += '<button class="page-btn' + (p === page ? ' active' : '') +
           '" data-p="' + p + '">' + (p + 1) + '</button>';
    }
    if (pages > 10) {
      s += '<span class="dim">…</span>' +
           '<button class="page-btn" data-p="' + (pages - 1) + '">' + pages + '</button>';
    }
    box.innerHTML = s;
    var btns = box.getElementsByTagName('button');
    for (var b = 0; b < btns.length; b++) {
      btns[b].onclick = function () {
        page = parseInt(this.getAttribute('data-p'), 10);
        render();
      };
    }
  }
  function render() {
    var a = page * PAGE, b = Math.min(O.length, a + PAGE), s = '';
    for (var k = a; k < b; k++) {
      var o = O[k];
      s += '<tr><td>' + esc(o.code) + '</td><td>' + esc(o.side) + '</td><td>' +
           esc(o.qty) + '</td><td>' + esc(o.status) + '</td><td>' + esc(o.at) + '</td></tr>';
    }
    tb.innerHTML = s;
    lbl.textContent = '第 ' + (page + 1) + ' / ' + pages + ' 页 · 共 ' + O.length + ' 条';
    prev.disabled = page === 0;
    next.disabled = page >= pages - 1;
    renderPages();
  }
  prev.onclick = function () { if (page > 0) { page--; render(); } };
  next.onclick = function () { if (page < pages - 1) { page++; render(); } };
  render();
})();
"""
