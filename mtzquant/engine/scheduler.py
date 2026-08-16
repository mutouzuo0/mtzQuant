# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : 调度降级规则（设计 5.2/4.7）：日线三时点/盘中折叠/拒绝语义纯逻辑

"""调度降级规则引擎（设计 5.2 / 4.7，纯逻辑、表驱动、无 I/O）。

日线模式只提供三类语义明确的时点：before_open / on_daily_close / after_close，
绝不假装精确（"宁可显式声明近似，不可静默假装精确"）：
  盘中 run_daily(09:30/14:50) + 日线 → 折叠 15:00 并记 semantic_degradation（对齐 PTrade 官方）；
  strict_schedule=true               → 拒绝（schedule_requires_minute_data）；
  run_interval（秒级周期）            → 结构化报错（官方仅实盘可用，4.7）；
  weekly/monthly                     → 折叠到周/月首个交易日 daily_close；
  timer（预留）                      → 拒绝（需分钟数据）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mtzquant.core.types import Frequency


class SessionTiming(StrEnum):
    """日线模式三类语义明确的时点（设计 5.2 降级规则第 1 条）。"""

    BEFORE_OPEN = "before_open"
    ON_DAILY_CLOSE = "on_daily_close"
    AFTER_CLOSE = "after_close"


class DegradationKind(StrEnum):
    """semantic_degradation 记录类别（报告列出全部降级项，第 9 章）。"""

    FOLD_MIDDAY_TO_CLOSE = "fold_midday_to_close"  # 盘中时刻 → 15:00 收盘
    FOLD_PERIOD_TO_FIRST_DAY = "fold_period_to_first_day"  # 周/月 → 首个交易日


@dataclass(frozen=True)
class DegradationNote:
    """一次降级记录：保留被折叠的原调度时刻（对齐官方行为、保证可对照）。"""

    kind: DegradationKind
    original_api: str
    original_time: str
    detail: str = ""


@dataclass(frozen=True)
class ScheduleResolution:
    """一条调度声明的解析结果（引擎按 timing 挂回调；rejected 一律不进回调）。"""

    timing: SessionTiming | None  # None：该调度不可用
    ok: bool
    error_code: str | None = None  # schedule_requires_minute_data / run_interval_unsupported
    degradation: DegradationNote | None = None

    @property
    def hint(self) -> str:
        if not self.ok:
            return {
                "schedule_requires_minute_data": "改分钟数据或改收盘调度（strict_schedule=true）",
                "run_interval_unsupported": "run_interval 仅实盘可用（PTrade 官方语义，4.7）",
            }.get(self.error_code or "", "")
        return ""


_DAILY_OK_TIMING: dict[str, SessionTiming] = {
    "handle_data": SessionTiming.ON_DAILY_CLOSE,
    "before_trading_start": SessionTiming.BEFORE_OPEN,
    "after_trading_end": SessionTiming.AFTER_CLOSE,
    "run_weekly": SessionTiming.ON_DAILY_CLOSE,  # 折叠首交易日（引擎按日历对齐）
    "run_monthly": SessionTiming.ON_DAILY_CLOSE,  # 折叠首交易日
}

_MIDDAY_FOLD_API = ("run_daily",)


def sees_previous_close_only(timing: SessionTiming) -> bool:
    """日线 before_open 时点仅见昨收：当日 OHLC 尚未形成（4.7 语义，杜绝静默用当日数据）。"""
    return timing is SessionTiming.BEFORE_OPEN


def resolve_schedule(
    freq: Frequency, api: str, time_str: str = "15:00", *, strict_schedule: bool = False
) -> ScheduleResolution:
    """表驱动解析一条调度声明（设计 5.2 降级规则 1-3 条）。"""
    if api == "run_interval":
        return ScheduleResolution(timing=None, ok=False, error_code="run_interval_unsupported")
    if freq in (Frequency.M1, Frequency.M5):
        # 分钟模式：任何 run_daily 时刻都精确；timer 仍按官方拒绝
        if api == "timer":
            return ScheduleResolution(
                timing=None, ok=False, error_code="schedule_requires_minute_data"
            )
        return ScheduleResolution(timing=SessionTiming.ON_DAILY_CLOSE, ok=True)

    # ---- 日线模式（Frequency.D1）----
    if api == "timer":
        return ScheduleResolution(timing=None, ok=False, error_code="schedule_requires_minute_data")
    if api == "run_weekly" or api == "run_monthly":
        return ScheduleResolution(
            timing=SessionTiming.ON_DAILY_CLOSE,
            ok=True,
            degradation=DegradationNote(
                kind=DegradationKind.FOLD_PERIOD_TO_FIRST_DAY,
                original_api=api,
                original_time="first_trading_day",
                detail="折叠到周期首个交易日 daily_close",
            ),
        )
    if api in _MIDDAY_FOLD_API:
        # 15:00 即默认收盘时点；其余盘中时刻做折叠/拒绝二择
        if time_str == "15:00":
            return ScheduleResolution(timing=SessionTiming.ON_DAILY_CLOSE, ok=True)
        if strict_schedule:
            return ScheduleResolution(
                timing=None, ok=False, error_code="schedule_requires_minute_data"
            )
        return ScheduleResolution(
            timing=SessionTiming.ON_DAILY_CLOSE,
            ok=True,
            degradation=DegradationNote(
                kind=DegradationKind.FOLD_MIDDAY_TO_CLOSE,
                original_api=api,
                original_time=time_str,
                detail=f"盘中 {time_str} 折叠到 15:00（对齐 PTrade 官方日线语义）",
            ),
        )
    return ScheduleResolution(
        timing=_DAILY_OK_TIMING.get(api, SessionTiming.ON_DAILY_CLOSE), ok=True
    )
