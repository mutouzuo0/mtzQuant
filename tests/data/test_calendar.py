# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 01:10:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D04：交易日历（设计 3.12-④）

"""T-D04：交易日历（设计 3.12-④）——文件/推导/回写/展开/越界。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.data.calendar import TradeCalendar

# 2024-01 沪深交易日（跳过周末）
_JAN24 = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
    date(2024, 1, 10),
    date(2024, 1, 11),
    date(2024, 1, 12),
    date(2024, 1, 15),
]


def test_from_dates_sorted_unique() -> None:
    cal = TradeCalendar.from_dates([_JAN24[3], _JAN24[0], _JAN24[0], _JAN24[1]])
    assert cal.size == 3
    assert cal.first_day == _JAN24[0]
    assert cal.last_day == _JAN24[3]


def test_csv_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cal = TradeCalendar.from_dates(_JAN24)
    p = tmp_path / "calendars" / "trade_days.csv"
    cal.save(p)
    loaded = TradeCalendar.from_csv(p)
    assert loaded._days == _JAN24


def test_from_csv_missing_file_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(MtzQuantError):
        TradeCalendar.from_csv(tmp_path / "nope.csv")


def test_from_csv_accepts_yyyymmdd(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "trade_days.csv"
    p.write_text("cal_date\n20240102\n20240103\n", encoding="utf-8")
    cal = TradeCalendar.from_csv(p)
    assert cal.size == 2
    assert cal.first_day == date(2024, 1, 2)


def test_derive_and_writeback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """缺失时由数据日并集推导并回写（3.12-④）。"""
    pool = [datetime(2024, 1, 2, 15, 0), datetime(2024, 1, 3, 15, 0), datetime(2024, 1, 4, 15, 0)]
    cal = TradeCalendar.derive(pool)
    assert cal.size == 3
    p = tmp_path / "trade_days.csv"
    cal.save(p)
    assert p.is_file()
    assert TradeCalendar.from_csv(p).size == 3


def test_derive_empty_raises() -> None:
    with pytest.raises(MtzQuantError):
        TradeCalendar.derive([])


def test_trading_days_range() -> None:
    cal = TradeCalendar.from_dates(_JAN24)
    days = cal.trading_days(date(2024, 1, 3), date(2024, 1, 9))
    assert days == _JAN24[1:6]
    # 含首尾; 周末夹在中间不出现
    assert all(d.weekday() < 5 for d in days)


def test_expand_warmup() -> None:
    cal = TradeCalendar.from_dates(_JAN24)
    # start=01-08, warmup=3 → 之前的 3 个交易日 [01-03,04,05]
    warm = cal.expand_warmup(date(2024, 1, 8), 3)
    assert warm == _JAN24[1:4]
    # warmup=0 → 空
    assert cal.expand_warmup(date(2024, 1, 8), 0) == []
    with pytest.raises(MtzQuantError):
        cal.expand_warmup(date(2024, 1, 8), -1)


def test_before_and_after() -> None:
    cal = TradeCalendar.from_dates(_JAN24)
    assert cal.before(date(2024, 1, 3)) == _JAN24[0]
    assert cal.after(date(2024, 1, 5)) == _JAN24[4]
    assert cal.before(_JAN24[0]) is None  # 无更早
    assert cal.after(_JAN24[-1]) is None  # 无更晚


def test_contains_and_out_of_range() -> None:
    cal = TradeCalendar.from_dates(_JAN24)
    assert cal.contains(date(2024, 1, 8))
    assert not cal.contains(date(2024, 1, 6))  # 周六
    cal.assert_in_range(_JAN24[0])  # 不抛
    with pytest.raises(MtzQuantError):
        cal.assert_in_range(date(2023, 12, 29))
    with pytest.raises(MtzQuantError):
        cal.assert_in_range(date(2024, 2, 1))
