# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 23:10:00
# @update_time        : 2026/08/16 23:10:00
# @description : M3-R2/R3 基本面与成分读取：FundamentalsStore（CSV → PIT 查询, 设计 3.13）

"""基本面/成分本地存储（设计 3.13 PIT 四时间, M3-R2 落盘布局 + R3 查询）。

落盘布局（R2, 3.12）:
  data/fundamentals/{table}/{code}.csv          主键 code+报告期+公告日（修订版本行保留）
    table ∈ fina_indicator | daily_basic
  data/index_constituents/{index}_{date}.csv    成分快照（in_date/out_date 区间字段）

PIT 查询（R3, 3.13 双时间校验）:
  fundamentals(code, table, fields, as_of, knowledge_time):
    校验 event_time <= as_of 且 available_at <= knowledge_time; available_at =
    published_at + delay_offset_days（供应商同步延迟可配, 默认 0）;
    **ann_date 缺失行拒绝**（fail-loud, 防前视偏差）;
  index_stocks(index, as_of): 取 ≤as_of 最近快照——**禁止用当前成分回填历史**（防幸存者偏差）。

确定性: 全部日期按 YYYY-MM-DD 文本比较/解析, 排序输出; 缺失文件 → 结构化错误。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from mtzquant.core.errors import MtzQuantError

# 财务表 -> (事件时间列=报告期/交易日, 公布列=公告日/同日)
# 落盘列名与 tushare 源一致（R1 源格式, 3.12 raw 精神; 读时本层解析）
TABLE_SPECS: dict[str, dict[str, str]] = {
    "fina_indicator": {"event_col": "end_date", "published_col": "ann_date"},
    "daily_basic": {"event_col": "trade_date", "published_col": "trade_date"},
    "dividend": {"event_col": "record_date", "published_col": "ann_date"},
}

# 各表默认可用字段（fields=None 时返回; 数值字段, 缺失列自动跳过）
TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "fina_indicator": ("netprofit_yoy", "or_yoy"),
    "daily_basic": ("pe", "pb", "total_mv", "circ_mv"),
    "dividend": ("record_date", "cash_div_tax", "ex_date", "div_proc"),
}

_SSE = "Asia/Shanghai"


def _parse_yyyymmdd(value: Any) -> date | None:
    """YYYYMMDD 整数/字符串 → date; 空/非法 → None（缺公告日拒绝由调用方判定）。"""
    text = str(value).strip()
    if not text or text in {"nan", "None", ""}:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


class FundamentalsStore:
    """基本面 + 成分快照的 PIT 读取（不可变: 构造后只读, 确定性 8.8）。"""

    def __init__(self, root_path: Path | str, *, delay_offset_days: int = 0) -> None:
        self._root = Path(root_path)
        self._offset = timedelta(days=max(0, delay_offset_days))
        self._cache: dict[str, pd.DataFrame] = {}  # 会话级 {path: df}

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def fundamentals_path(self, table: str, code: str) -> Path:
        if table not in TABLE_SPECS:
            raise MtzQuantError(
                f"未知财务表 {table!r}", stage="fundamentals", hint=f"可选: {sorted(TABLE_SPECS)}"
            )
        return self._root / "fundamentals" / table / f"{code}.csv"

    def constituents_dir(self) -> Path:
        return self._root / "index_constituents"

    # ------------------------------------------------------------------
    # 基本面 PIT 查询（R3, 3.13 双时间）
    # ------------------------------------------------------------------
    def fundamentals(
        self,
        code: str,
        table: str,
        fields: list[str] | None,
        *,
        as_of: datetime,
        knowledge_time: datetime | None = None,
    ) -> pd.DataFrame:
        """可见基本面行（event_time<=as_of 且 available_at<=knowledge_time）。

        返回: 索引 = event_time（Asia/Shanghai tz-aware, 报告期/交易日 00:00）;
              列 = 请求字段（数值）。**ann_date 缺失行拒绝入库**（fail-loud, R1 决策）。
        """
        if table not in TABLE_SPECS:
            raise MtzQuantError(
                f"未知财务表 {table!r}", stage="fundamentals", hint=f"可选: {sorted(TABLE_SPECS)}"
            )
        if knowledge_time is None:
            knowledge_time = as_of
        # 日线粒度数据按「墙钟日」比较: 剥离时区（与 datetime64 无 tz 对齐, 8.8 确定性）
        as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo is not None else as_of
        kt_naive = (
            knowledge_time.replace(tzinfo=None)
            if knowledge_time.tzinfo is not None
            else knowledge_time
        )
        raw = self._read_fundamentals(table, code)
        if raw is None or raw.empty:
            return _empty_fund_frame(fields)
        spec = TABLE_SPECS[table]
        ev_col, pub_col = spec["event_col"], spec["published_col"]
        events = pd.to_datetime(raw[ev_col], format="%Y%m%d", errors="coerce")
        pubs = pd.to_datetime(raw[pub_col], format="%Y%m%d", errors="coerce")
        # ann_date 缺失行拒绝（fail-loud: 无披露日的财务数据不可信, 防前视偏差）
        missing = int(pubs.isna().sum())
        if missing:
            raise MtzQuantError(
                f"财务数据 {code}/{table} 含 {missing} 行缺 {pub_col}（披露日）",
                stage="fundamentals",
                hint="ann_date 缺失行拒绝入库（R1 决策）; 检查数据源/落盘完整性",
            )
        mask_ev = events <= as_of_naive
        mask_pub = pubs + pd.Timedelta(days=self._offset.days) <= kt_naive
        keep = mask_ev & mask_pub
        if not keep.any():
            return _empty_fund_frame(fields)
        sub = raw.loc[keep].copy()
        out = pd.DataFrame(index=events[keep])
        want = list(fields) if fields else list(TABLE_FIELDS.get(table, ()))
        # 按位置赋值（out 行序与 keep 一致; 避免 pandas index 对齐导致 NaN）
        for f in want:
            if f in sub.columns:
                out[f] = pd.to_numeric(sub[f].to_numpy(), errors="coerce")
        out.index = pd.to_datetime(events[keep]).dt.tz_localize(_SSE)
        out.index.name = "dt"
        return out.sort_index()

    def _read_fundamentals(self, table: str, code: str) -> pd.DataFrame | None:
        path = self.fundamentals_path(table, code)
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        if not path.is_file():
            return None
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except OSError as exc:
            raise MtzQuantError(
                f"基本面读取失败: {path}", stage="fundamentals", hint=f"原因为 {exc}"
            ) from exc
        self._cache[key] = df
        return df

    # ------------------------------------------------------------------
    # 成分快照 PIT 查询（R3: 取 ≤as_of 最近快照, 防幸存者偏差）
    # ------------------------------------------------------------------
    def list_snapshots(self, index: str) -> list[tuple[date, Path]]:
        """该指数全部成分快照 (日期, 路径), 升序。"""
        out: list[tuple[date, Path]] = []
        d = self.constituents_dir()
        if not d.is_dir():
            return out
        prefix = f"{index}_"
        for f in sorted(d.glob(f"{prefix}*.csv")):
            stem = f.name[len(prefix) : -len(".csv")]
            snap = _parse_yyyymmdd(stem)
            if snap is not None:
                out.append((snap, f))
        return out

    def index_stocks(self, index: str, as_of: datetime) -> list[str]:
        """≤as_of 最近快照的成分代码（升序; 无快照 → 结构化错误）。

        **防幸存者偏差**: 取历史快照而非「当前成分回填历史」——T-R03 反例断言。
        """
        snaps = [s for s in self.list_snapshots(index) if s[0] <= as_of.date()]
        if not snaps:
            raise MtzQuantError(
                f"无 {index} 在 {as_of.date()} 前的成分快照",
                stage="fundamentals",
                hint="先运行 `mtzquant fetch --constituents`（3.13 成分快照下载）",
            )
        snap_date, path = snaps[-1]
        df = self._read_snapshot(path)
        members = [str(c) for c in df["con_code"].tolist() if str(c).strip()]
        if not members:
            raise MtzQuantError(
                f"成分快照为空: {path}", stage="fundamentals", hint="检查快照内容与列名"
            )
        # 快照含入/出日期时, 按 (in_date,out_date) 过滤出该时刻仍有效的成分（3.13 区间字段）
        filtered = self._filter_members(df, as_of.date())
        out = filtered or members
        return sorted(out)

    @staticmethod
    def _filter_members(df: pd.DataFrame, as_of: date) -> list[str]:
        """按 in_date/out_date 区间过滤当日有效成分; 区间字段缺失 → 返回 []（交给调用方退化）。"""
        if "in_date" not in df.columns or "out_date" not in df.columns:
            return []
        if df["in_date"].astype(str).str.strip().isin(["", "nan", "None"]).all():
            return []
        keep: list[str] = []
        for _, row in df.iterrows():
            in_d = _parse_yyyymmdd(row.get("in_date"))
            out_d = _parse_yyyymmdd(row.get("out_date"))
            if in_d is None or in_d > as_of:
                continue
            if out_d is not None and out_d <= as_of:
                continue
            keep.append(str(row["con_code"]))
        return keep

    def _read_snapshot(self, path: Path) -> pd.DataFrame:
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except OSError as exc:
            raise MtzQuantError(
                f"成分快照读取失败: {path}", stage="fundamentals", hint=f"原因为 {exc}"
            ) from exc
        if "con_code" not in df.columns:
            raise MtzQuantError(
                f"成分快照缺 con_code 列: {path}",
                stage="fundamentals",
                hint="快照列: index_code/con_code/in_date/out_date/weight（R2 布局）",
            )
        self._cache[key] = df
        return df


def _empty_fund_frame(fields: list[str] | None) -> pd.DataFrame:
    cols = list(fields) if fields else []
    return pd.DataFrame(columns=cols)
