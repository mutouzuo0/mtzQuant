# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:26:00
# @update_time        : 2026/08/16 21:59:08
# @description : D4 交易日历：文件/推导回写/预热展开（设计 3.12-④）

"""交易日历（设计 3.12-④）——SSE/SZSE 交易日的唯一日历源。

存储: calendars/trade_days.csv（单列日期, YYYY-MM-DD）。
   缺失时：由已加载股票池的数据日并集推导, 并回写缓存（3.12-④ 兜底逻辑）。
   越界: 区间落在无数据处返回空, 单点查询越界抛结构化错误（预览期防『无数据回测』闷报）。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from mtzquant.core.errors import MtzQuantError

_DATE_FMT = "%Y-%m-%d"


class TradeCalendar:
    """交易日历（immutable: 构造后不可再变——确定性纪律 8.8）。"""

    def __init__(self, days: list[date]) -> None:
        self._days: list[date] = sorted({d for d in days if d is not None})
        self._set = set(self._days)

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_csv(cls, path: Path | str) -> TradeCalendar:
        """从 trade_days.csv 读取（列名自动兼容 date/cal_date/YYYYMMDD）。"""
        p = os.fspath(Path(path))
        try:
            df = pd.read_csv(p)
        except FileNotFoundError as exc:
            raise MtzQuantError(
                f"交易日历文件不存在: {p}",
                stage="calendar",
                hint="calendars/trade_days.csv 缺失时可用 from_dates()/derive_and_save() 推导",
            ) from exc
        col = next((c for c in ("date", "cal_date", "trade_date") if c in df.columns), None)
        if col is None:
            raise MtzQuantError(
                f"交易日历列名未识别（列: {list(df.columns)}）",
                stage="calendar",
                hint="expect 单列 date（YYYY-MM-DD）或 cal_date/trade_date（YYYYMMDD）",
            )
        raw = df[col].astype(str).str.strip()
        if raw.str.fullmatch(r"\d{8}").any():
            days = pd.to_datetime(raw, format="%Y%m%d", errors="coerce").dt.date
        else:
            days = pd.to_datetime(raw, format="%Y-%m-%d", errors="coerce").dt.date
        return cls([d for d in days.tolist() if d is not None])

    @classmethod
    def from_dates(cls, dates: list[date | datetime]) -> TradeCalendar:
        """从任意日期列表构造（datetime 取 .date()）。"""
        return cls([d.date() if isinstance(d, datetime) else d for d in dates])

    # ------------------------------------------------------------------
    # 推导与回写（3.12-④: 缺失时由数据日并集推导）
    # ------------------------------------------------------------------
    @classmethod
    def derive(cls, pool_dates: list[date | datetime]) -> TradeCalendar:
        """由已加载股票池的数据日并集推导交易日历（去重升序）。"""
        if not pool_dates:
            raise MtzQuantError(
                "空数据日集合无法推导交易历",
                stage="calendar",
                hint="先加载至少一个标的的 K 线，或提供 calendars/trade_days.csv",
            )
        return cls.from_dates(pool_dates)

    def save(self, path: Path | str) -> None:
        """回写 calendars/trade_days.csv（原子写: 临时文件 + os.replace）。"""
        import tempfile

        p = os.fspath(Path(path))
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".csv.tmp", dir=os.path.dirname(p) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write("date\n")
                for d in self._days:
                    fh.write(d.strftime(_DATE_FMT) + "\n")
            os.replace(tmp, p)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._days)

    @property
    def first_day(self) -> date | None:
        return self._days[0] if self._days else None

    @property
    def last_day(self) -> date | None:
        return self._days[-1] if self._days else None

    def contains(self, day: date | datetime) -> bool:
        d = day.date() if isinstance(day, datetime) else day
        return d in self._set

    def trading_days(self, start: date | datetime, end: date | datetime) -> list[date]:
        """区间 [start, end]（含首尾）内的全部交易日。"""
        from bisect import bisect_left, bisect_right

        lo = bisect_left(self._days, start.date() if isinstance(start, datetime) else start)
        hi = bisect_right(self._days, end.date() if isinstance(end, datetime) else end)
        return self._days[lo:hi]

    def expand_warmup(self, start: date | datetime, warmup_bars: int) -> list[date]:
        """返回 [start-warmup_bars 个交易日, start) 的预热区间交易日（不含起始日）。"""
        if warmup_bars < 0:
            raise MtzQuantError(f"warmup_bars 不能为负，得到 {warmup_bars}", stage="calendar")
        start_d = start.date() if isinstance(start, datetime) else start
        from bisect import bisect_left

        hi = bisect_left(
            self._days, start_d
        )  # 严格早于起始日（起始日 bar 属于回测第 1 根, 不属预热）
        lo = max(0, hi - warmup_bars)
        return self._days[lo:hi]

    def before(self, day: date | datetime) -> date | None:
        """严格早于该日的最近交易日（盘前可见昨日收盘）。"""
        from bisect import bisect_left

        d = day.date() if isinstance(day, datetime) else day
        idx = bisect_left(self._days, d)
        return self._days[idx - 1] if idx > 0 else None

    def after(self, day: date | datetime) -> date | None:
        """严格晚于该日的最近交易日（下一交易日 09:30 成交时点用）。"""
        from bisect import bisect_right

        d = day.date() if isinstance(day, datetime) else day
        idx = bisect_right(self._days, d)
        return self._days[idx] if idx < len(self._days) else None

    def assert_in_range(self, day: date) -> None:
        """单点越界报错（预览期防『无数据回测』闷报, 设计 3.12-④）。"""
        if not self._set:
            raise MtzQuantError(
                "交易日历为空", stage="calendar", hint="需先推导或提供 trade_days.csv"
            )
        if not (self._days[0] <= day <= self._days[-1]):
            raise MtzQuantError(
                f"日期 {day} 超出交易日历范围 [{self._days[0]}, {self._days[-1]}]",
                stage="calendar",
                hint="检查回测区间与数据覆盖范围",
            )
