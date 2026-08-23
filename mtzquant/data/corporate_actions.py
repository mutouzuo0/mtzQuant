# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/23 11:30:00
# @update_time        : 2026/08/23 12:30:00
# @description : 共享公司行为日历（3.12）: 加载器 + fetch 自动灌入（拆股扫描 + fund_div 分红）

"""共享公司行为日历（独立于价格数据、独立于策略的通用市场数据表）。

目录布局（设计 3.12 / AGENTS.md 数据目录约定）:
    data/corporate_actions/{type}/{code}.csv   # type ∈ split/cash_div/bonus

每个文件列（列名大小写不敏感, 缺失列忽略）:
    announce_date, ex_date, pay_date, per_share_cash, ratio
（code/type 从文件路径推导: {code}.csv 文件名 + 上级目录 {type}）

本模块只做「读文件 → 行字典」, 不 import engine（依赖纪律: data 层禁止依赖 engine）;
行字典由引擎层（BacktestSession._parse_corp_action）转 CorporateAction 生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from mtzquant.core.codes import normalize_code

# 列别名: 规范名 → 兼容别名集合（trade_days 同款"列名不敏感"约定）
_COL_ALIASES: dict[str, tuple[str, ...]] = {
    "announce_date": ("announce_date", "announce", "ann_dt", "公告日"),
    "ex_date": ("ex_date", "ex_dt", "exdate", "除权日"),
    "pay_date": ("pay_date", "pay_dt", "paydate", "到账日"),
    "per_share_cash": ("per_share_cash", "cash", "cash_per_share", "每股现金"),
    "ratio": ("ratio", "split_ratio", "送转比例"),
}


def _pick(row: pd.Series, key: str) -> Any:
    """按别名取单元格; 空串 → None。"""
    for alias in _COL_ALIASES[key]:
        if alias in row.index:
            v = row[alias]
            if pd.notna(v) and str(v).strip() != "":
                return str(v).strip()
    return None


def load_corporate_actions(
    root: Path, dir_template: str = "corporate_actions/{type}"
) -> list[dict[str, Any]]:
    """读取共享公司行为日历, 返回行字典列表（code/type/三日期/现金/比例）。

    `dir_template` 形如 `corporate_actions/{type}`（settings.local_csv.corporate_actions_dir）;
    `{type}` 占位剥离后 rglob 全量 .csv——目录缺失/空 → []。
    """
    base = Path(str(dir_template).split("{type}")[0].strip("/\\"))
    d = root / base
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(d.rglob("*.csv")):
        act_type = path.parent.name  # .../corporate_actions/{type}/{code}.csv
        code = path.stem
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except (OSError, ValueError, KeyError):
            continue  # 单文件坏行不阻断整表（best-effort, 3.12）
        for _, row in df.iterrows():
            item: dict[str, Any] = {
                "code": code,
                "type": act_type,
                "announce_date": _pick(row, "announce_date"),
                "ex_date": _pick(row, "ex_date"),
                "pay_date": _pick(row, "pay_date"),
                "per_share_cash": _pick(row, "per_share_cash"),
                "ratio": _pick(row, "ratio"),
            }
            out.append(item)
    return out


# ------------------------------------------------------------------
# fetch 侧: 自动灌入共享日历（本地拆股扫描 + tushare fund_div 分红, 3.12）
# ------------------------------------------------------------------
@dataclass(frozen=True)
class CorpActionFetchReport:
    """单标的公司行为下载报告。"""

    code: str
    splits: int = 0  # 扫描到的拆股事件数
    dividends: int = 0  # 拉取到的分红事件数
    status: str = "ok"  # ok | skipped | failed
    reason: str = ""


def _snap_ratio(ratio: float) -> float:
    """把反推比例吸附到标准拆股比例（2/3/5, 容忍 ±0.3）。"""
    for std in (5.0, 3.0, 2.0):
        if abs(ratio - std) < 0.3:
            return std
    return round(ratio, 1)


def discover_splits(
    root: Path, codes: list[str], start: date, end: date, *, threshold: float = 0.25
) -> dict[str, list[dict[str, Any]]]:
    """本地日线扫描拆股（份额折算）：除权日开盘较前收盘跌幅 > threshold。

    比例 = 前收盘 / 除权日开盘（吸附到 2/3/5）; announce = 前一交易日（日历优先）。
    仅扫描 ETF 日线目录（data/kline/etf/day/{code}.csv）。
    """
    kline_dir = root / "kline" / "etf" / "day"
    if not kline_dir.is_dir():
        return {}
    cal = _load_calendar_days(root)
    out: dict[str, list[dict[str, Any]]] = {}
    s0 = start.strftime("%Y%m%d")
    e0 = end.strftime("%Y%m%d")
    for code in sorted({normalize_code(c) for c in codes}):
        path = kline_dir / f"{code}.csv"
        if not path.is_file():
            continue
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except (OSError, ValueError):
            continue
        for col in ("trade_date", "open", "close"):
            if col not in df.columns:
                continue
        df = df[df["trade_date"].between(s0, e0)].copy()
        if df.empty:
            continue
        df["open"] = df["open"].astype(float)
        df["close"] = df["close"].astype(float)
        df["prev"] = df["close"].shift(1)
        df["drop"] = df["open"] / df["prev"] - 1.0
        for _, r in df[df["drop"] < -threshold].iterrows():
            ex = r["trade_date"]
            ratio = _snap_ratio(r["prev"] / r["open"])
            ann = _prev_td(cal, ex) or ex
            out.setdefault(code, []).append(
                {
                    "announce_date": f"{ann[:4]}-{ann[4:6]}-{ann[6:]}",
                    "ex_date": f"{ex[:4]}-{ex[4:6]}-{ex[6:]}",
                    "ratio": ratio,
                }
            )
    return out


def _load_calendar_days(root: Path) -> list[str]:
    """升序交易日（YYYYMMDD）; 缺失 → []（退化为纯周历不取）。"""
    path = root / "calendars" / "trade_days.csv"
    if not path.is_file():
        return []
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        names = ("date", "trade_date", "trading_day")
        col = next((c for c in df.columns if c.lower() in names), df.columns[0])
        days = sorted(
            pd.to_datetime(df[col], errors="coerce").dropna().dt.strftime("%Y%m%d").tolist()
        )
        return days
    except (OSError, ValueError, KeyError, IndexError):
        return []


def _prev_td(cal: list[str], d: str) -> str | None:
    if not cal:
        return None
    idx = None
    for i, x in enumerate(cal):
        if x == d:
            idx = i
            break
    if idx is None or idx == 0:
        return None
    return cal[idx - 1]


def fetch_dividends(
    codes: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
    fetch_fn: Any | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """tushare fund_div（基金分红）→ 分红事件（ann/ex/pay + 每份现金, 去重）。

    fetch_fn(code) → DataFrame（测试注入）; 缺省走 tushare 源。仅收录 div_proc=实施。
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for code in sorted({normalize_code(c) for c in codes}):
        try:
            if fetch_fn is not None:
                df = fetch_fn(code)
            else:
                df = _fetch_fund_div(code)
        except Exception:  # noqa: BLE001 — 单标的失败不阻断整批
            out.setdefault(code, [])
            continue
        if df is None or df.empty:
            continue
        if "div_proc" in df.columns:
            df = df[df["div_proc"].astype(str).str.contains("实施", na=False)]
        seen: set[tuple[str, str, float]] = set()
        events: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            ex = str(r.get("ex_date") or "").strip()
            ann = str(r.get("ann_date") or "").strip()
            pay = str(r.get("pay_date") or "").strip()
            cash = r.get("div_cash")
            if not ex:
                continue
            try:
                cash_v = float(cash)
            except (TypeError, ValueError):
                continue
            if start is not None and ex <= start.strftime("%Y%m%d"):
                continue
            if end is not None and ex > end.strftime("%Y%m%d"):
                continue
            key = (ex, ann, cash_v)
            if key in seen:
                continue
            seen.add(key)
            item: dict[str, Any] = {
                "announce_date": f"{ann[:4]}-{ann[4:6]}-{ann[6:]}" if len(ann) == 8 else ann,
                "ex_date": f"{ex[:4]}-{ex[4:6]}-{ex[6:]}" if len(ex) == 8 else ex,
                "pay_date": f"{pay[:4]}-{pay[4:6]}-{pay[6:]}" if len(pay) == 8 else pay,
                "per_share_cash": cash_v,
            }
            events.append(item)
        if events:
            out[code] = events
    return out


def _fetch_fund_div(code: str) -> pd.DataFrame:
    """tushare pro.fund_div（真实网络; 未配 token 抛结构化错误）。"""
    import tushare as ts  # 可选依赖（extras=[download]）

    from mtzquant.config import get_tushare_token

    pro = ts.pro_api(get_tushare_token())
    return pro.fund_div(ts_code=code)


def _norm_cell(v: Any) -> str:
    """去重键单元格归一（None/NaN/空串 → ''; 数字统一字符串, 避免 0.142 vs '0.142' 不匹配）。"""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:  # NaN
        return ""
    s = str(v).strip()
    return "" if s == "nan" else s


def _group_by_type(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按事件类型分组: 有 per_share_cash → cash_div; 有 ratio → split（互斥, 防混写）。"""
    out: dict[str, list[dict[str, Any]]] = {"split": [], "cash_div": []}
    for e in events:
        if _norm_cell(e.get("per_share_cash")):
            out["cash_div"].append(e)
        elif _norm_cell(e.get("ratio")):
            out["split"].append(e)
    return out


def write_corporate_actions(root: Path, events_by_code: dict[str, list[dict[str, Any]]]) -> int:
    """事件按 {type}/{code}.csv 落盘（拆股/分红分目录, 与已有内容合并去重; 返回新增文件数）。"""
    n_files = 0
    for code, events in sorted(events_by_code.items()):
        for act_type, group in _group_by_type(events).items():
            if not group:
                continue
            d = root / "corporate_actions" / act_type
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{code}.csv"
            cols = ["announce_date", "ex_date", "pay_date", "per_share_cash", "ratio"]
            rows: list[dict[str, Any]] = []
            if path.is_file():
                try:
                    rows = pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records")
                except (OSError, ValueError):
                    rows = []
            known = {
                (
                    _norm_cell(r.get("ex_date")),
                    _norm_cell(r.get("announce_date")),
                    _norm_cell(r.get("per_share_cash")),
                    _norm_cell(r.get("ratio")),
                )
                for r in rows
            }
            for e in group:
                key = (
                    _norm_cell(e.get("ex_date")),
                    _norm_cell(e.get("announce_date")),
                    _norm_cell(e.get("per_share_cash")),
                    _norm_cell(e.get("ratio")),
                )
                if key in known:
                    continue
                rows.append(e)
                known.add(key)
            out_df = pd.DataFrame(rows, columns=cols)
            out_df.to_csv(path, index=False, encoding="utf-8")
            n_files += 1
    return n_files


def fetch_corporate_actions(
    root: Path,
    codes: list[str],
    start: date,
    end: date,
    *,
    split_threshold: float = 0.25,
    fetch_fn: Any | None = None,
) -> list[CorpActionFetchReport]:
    """自动灌入共享公司行为日历：本地拆股扫描 + tushare fund_div 分红。

    返回逐标的报告（拆股/分红事件数）。不抛出——单标的失败记录 status=failed。
    """
    splits = discover_splits(root, codes, start, end, threshold=split_threshold)
    divs = fetch_dividends(codes, start=start, end=end, fetch_fn=fetch_fn)
    reports: list[CorpActionFetchReport] = []
    for code in sorted({normalize_code(c) for c in codes}):
        s_events = splits.get(code, [])
        d_events = divs.get(code, [])
        if not s_events and not d_events:
            reports.append(
                CorpActionFetchReport(code=code, status="skipped", reason="无拆股/分红事件")
            )
            continue
        merged: dict[str, list[dict[str, Any]]] = {code: s_events + d_events}
        write_corporate_actions(root, merged)
        reports.append(
            CorpActionFetchReport(code=code, splits=len(s_events), dividends=len(d_events))
        )
    return reports
