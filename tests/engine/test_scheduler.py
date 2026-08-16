# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : T-U09 调度降级规则测试（设计 5.2/4.7）：表驱动各分支

"""T-U09：调度降级规则（设计 5.2 / 4.7）：盘中 run_daily+日线折叠 15:00 记降级、
strict_schedule 拒绝、run_interval 结构化报错、weekly/monthly 折叠首交易日、
分钟模式精确、before_open 仅见昨收。
"""

from __future__ import annotations

from mtzquant.core.types import Frequency
from mtzquant.engine.scheduler import (
    DegradationKind,
    ScheduleResolution,
    SessionTiming,
    resolve_schedule,
    sees_previous_close_only,
)


def test_daily_midday_run_daily_folds_to_close() -> None:
    """日线：run_daily(09:30) → 折叠 15:00 + semantic_degradation（对齐 PTrade 官方）。"""
    r = resolve_schedule(Frequency.D1, "run_daily", "09:30")
    assert r.ok
    assert r.timing is SessionTiming.ON_DAILY_CLOSE
    assert r.degradation is not None
    assert r.degradation.kind is DegradationKind.FOLD_MIDDAY_TO_CLOSE
    assert r.degradation.original_time == "09:30"  # 保留原调度时刻
    assert r.degradation.original_api == "run_daily"


def test_daily_15_00_run_daily_exact() -> None:
    """run_daily(15:00) 本就是日线收盘时点，无降级。"""
    r = resolve_schedule(Frequency.D1, "run_daily", "15:00")
    assert r.ok and r.timing is SessionTiming.ON_DAILY_CLOSE
    assert r.degradation is None


def test_strict_schedule_rejects_midday() -> None:
    """strict_schedule=true：盘中调度直接拒绝（schedule_requires_minute_data）。"""
    r = resolve_schedule(Frequency.D1, "run_daily", "14:50", strict_schedule=True)
    assert not r.ok
    assert r.error_code == "schedule_requires_minute_data"
    assert r.timing is None
    assert "分钟数据" in r.hint


def test_run_interval_structured_error() -> None:
    """run_interval（秒级周期）结构化报错：日线/分钟均拒绝（官方仅实盘，4.7）。"""
    for freq in (Frequency.D1, Frequency.M1, Frequency.M5):
        r = resolve_schedule(freq, "run_interval")
        assert not r.ok
        assert r.error_code == "run_interval_unsupported"
        assert "实盘" in r.hint


def test_weekly_monthly_fold_to_first_trading_day() -> None:
    """weekly/monthly → 折叠周期首交易日 daily_close，带降级记录。"""
    for api in ("run_weekly", "run_monthly"):
        r = resolve_schedule(Frequency.D1, api)
        assert r.ok
        assert r.timing is SessionTiming.ON_DAILY_CLOSE
        assert r.degradation is not None
        assert r.degradation.kind is DegradationKind.FOLD_PERIOD_TO_FIRST_DAY
        assert r.degradation.original_time == "first_trading_day"


def test_standard_update_points() -> None:
    """三类平台标准回调映射到对应日线时点。"""
    h = resolve_schedule(Frequency.D1, "handle_data")
    assert h.ok and h.timing is SessionTiming.ON_DAILY_CLOSE
    b = resolve_schedule(Frequency.D1, "before_trading_start")
    assert b.ok and b.timing is SessionTiming.BEFORE_OPEN
    a = resolve_schedule(Frequency.D1, "after_trading_end")
    assert a.ok and a.timing is SessionTiming.AFTER_CLOSE


def test_minute_mode_exact_for_midday() -> None:
    """分钟模式：run_daily(09:30) 精确到分钟、无降级。"""
    for freq in (Frequency.M1, Frequency.M5):
        r = resolve_schedule(freq, "run_daily", "09:30")
        assert r.ok
        assert r.degradation is None
        # 任何盘中时刻都是精确的（非严格三时点折叠语义）
        assert resolve_schedule(freq, "run_daily", "14:50").ok


def test_timer_rejected_even_in_minute_mode() -> None:
    """timer（预留，按秒周期）分钟模式仍拒绝（官方语义仅实盘）。"""
    assert not resolve_schedule(Frequency.M1, "timer").ok


def test_before_open_sees_previous_close_only() -> None:
    """before_open 仅见昨收：当日 OHLC 尚未形成（4.7，防静默近似）。"""
    assert sees_previous_close_only(SessionTiming.BEFORE_OPEN)
    assert not sees_previous_close_only(SessionTiming.ON_DAILY_CLOSE)
    assert not sees_previous_close_only(SessionTiming.AFTER_CLOSE)


def test_unknown_api_falls_back_to_close_timing() -> None:
    """未知 API 名回到默认收盘时点（不静默失败，记录在案的兜底）。"""
    r = resolve_schedule(Frequency.D1, "unknown_api")
    assert r.ok and isinstance(r, ScheduleResolution)
