# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 14:45:00
# @update_time        : 2026/08/16 21:59:08
# @description : O3 CoverageChecker：每标的覆盖区间/缺失段 gaps/重复日体检（3.9-①）

"""覆盖检查（设计 3.9-①）——增量下载前算出「请求区间 − 已覆盖」的缺失段。

- `coverage(code)`: min/max/dt count/distinct（重复 dt 数 = count − distinct, 进体检）;
- `covered_ranges(code)`: 按连续日历日切分已覆盖区间;
- `gaps(code, start, end)`: 请求区间减覆盖、含空洞切分（如 2020~2021 与 2023 已覆盖,
  2022 整年一个 gap）; 文件缺失/空文件 = 全缺失（gaps = [start, end]）。

只做「读日期列 + 集合运算」——不碰行情内容（质量体检归 duckdb_query, O2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from mtzquant.core.errors import MtzQuantError


@dataclass(frozen=True)
class InstrumentCoverage:
    """单标的覆盖摘要（3.9-①; dup = count − distinct）。"""

    code: str
    count: int = 0
    distinct_dt: int = 0
    min_dt: date | None = None
    max_dt: date | None = None

    @property
    def dup_dt(self) -> int:
        return self.count - self.distinct_dt

    @property
    def empty(self) -> bool:
        return self.distinct_dt == 0


class CoverageChecker:
    """本地 K 线覆盖检查（kline/{type}/day/{code}.csv, 3.12 布局）。"""

    def __init__(
        self, root_path: Path | str, *, instrument_type: str = "etf", frequency: str = "day"
    ) -> None:
        self._root = Path(root_path)
        self._type = instrument_type
        self._freq = frequency
        self._calendar: set[date] | None = None

    # ------------------------------------------------------------------
    def kline_path(self, code: str) -> Path:
        return self._root / "kline" / self._type / self._freq / f"{code}.csv"

    def _read_dates(self, code: str) -> list[date]:
        """读取已解析、去重、升序的日期列表（缺失/空文件 → []）。"""
        path = self.kline_path(code)
        if not path.is_file():
            return []
        try:
            df = pd.read_csv(path, usecols=["trade_date"], dtype=str, keep_default_na=False)
        except (ValueError, KeyError) as exc:
            raise MtzQuantError(
                f"覆盖检查读取失败: {code}",
                stage="coverage",
                hint=f"{exc}; 期望 tushare 源格式含 trade_date 列（3.5）",
            ) from exc
        if df.empty:
            return []
        dts = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce").dropna()
        out = sorted({d.date() for d in dts})
        return out

    # ------------------------------------------------------------------
    def coverage(self, code: str) -> InstrumentCoverage:
        dates = self._read_dates(code)
        if not dates:
            return InstrumentCoverage(code=code)
        return InstrumentCoverage(
            code=code,
            count=len(dates),
            distinct_dt=len(dates),
            min_dt=dates[0],
            max_dt=dates[-1],
        )

    def covered_ranges(self, code: str) -> list[tuple[date, date]]:
        """已覆盖区间: 连续日历日（相邻 +1 天）归为一段。"""
        return _contiguous_ranges(self._read_dates(code))

    def gaps(self, code: str, start: date, end: date) -> list[tuple[date, date]]:
        """请求区间 [start,end] 减已覆盖 → 缺失段列表（空洞切分, 3.9-①）。

        期望日 = 交易日历（data/calendars/trade_days.csv, 3.12）优先, 缺省退化为
        周一~周五近似——周末/节假日不构成「缺失」（保证重复 fetch 幂等, O 验收）。
        缺口按**期望交易日相邻**分组（覆盖日即断开）——周五→下周一仍是同一段,
        不会因周末被错误切碎成 5 天小片。
        """
        if start > end:
            raise MtzQuantError(
                f"请求区间非法: {start} > {end}", stage="coverage", hint="检查 --start/--end"
            )
        covered = set(self._read_dates(code))
        expected = sorted(self._expected_days(start, end))
        runs: list[tuple[date, date]] = []
        run_lo: date | None = None
        prev_missing: date | None = None
        for d in expected:
            if d in covered:
                if run_lo is not None and prev_missing is not None:
                    runs.append((run_lo, prev_missing))
                    run_lo = None
                continue
            if run_lo is None:
                run_lo = d
            prev_missing = d
        if run_lo is not None and prev_missing is not None:
            runs.append((run_lo, prev_missing))
        return runs

    def _expected_days(self, start: date, end: date) -> set[date]:
        """区间内期望交易日（日历优先; 退化为周一~周五）。"""
        cal = self._load_calendar()
        if cal:
            return {d for d in cal if start <= d <= end}
        out: set[date] = set()
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.add(d)
            d += timedelta(days=1)
        return out

    def day_count(self, start: date, end: date) -> int:
        """区间内期望交易日数量（P2-2: 覆盖报告「将下载 N 天」的统计口径）。"""
        return len(self._expected_days(start, end))

    def _load_calendar(self) -> set[date]:
        """读 data/calendars/trade_days.csv（列名不敏感: date/trade_date/trading_day）。"""
        if self._calendar is not None:
            return self._calendar
        path = self._root / "calendars" / "trade_days.csv"
        out: set[date] = set()
        if path.is_file():
            try:
                df = pd.read_csv(path, dtype=str, keep_default_na=False)
                col = next(
                    (c for c in df.columns if c.lower() in ("date", "trade_date", "trading_day")),
                    df.columns[0],
                )
                dts = pd.to_datetime(df[col], errors="coerce").dropna()
                out = {d.date() for d in dts}
            except (OSError, ValueError, IndexError):
                out = set()
        self._calendar = out
        return out

    # ------------------------------------------------------------------
    def health(self, codes: list[str]) -> list[InstrumentCoverage]:
        """批量覆盖摘要（含重复 dt 计数, 3.9-① 体检）。"""
        return [self.coverage(code) for code in codes]


def _contiguous_ranges(days: list[date]) -> list[tuple[date, date]]:
    """升序日期列表 → 连续日历日区间（相邻 +1 天归一段）。"""
    if not days:
        return []
    ranges: list[tuple[date, date]] = []
    start = prev = days[0]
    for d in days[1:]:
        if d == prev + timedelta(days=1):
            prev = d
            continue
        ranges.append((start, prev))
        start = prev = d
    ranges.append((start, prev))
    return ranges
