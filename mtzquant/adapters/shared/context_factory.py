# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:46:00
# @update_time        : 2026/08/16 09:46:00
# @description : K3 context 工厂（4.4/4.6/4.7）：内核通用字段 + 平台扩展槽, 每 bar 刷新

"""context 工厂（设计 4.4）——策略可见 context 的内核字段 + 平台扩展槽。

内核通用字段（两平台共 6 项）: current_dt / previous_date / universe / portfolio /
timestamp / run_id;
平台扩展槽: PTrade 按 4.7 补 capital_base/sim_params/initialized/slippage/commission/
recorded_vars/blotter.current_dt; 聚宽直接消费内核 current_dt（4.6）。

每 bar 由引擎刷新: `refresh_context` 原地更新可变字段（current_dt/previous_date/
universe/portfolio）, 平台适配器在 on_bar/on_before_trading 前调用（4.4）。
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

# 内核通用字段（设计 4.4）; 平台扩展用 setattr 直接挂载, 不锁表
KERNEL_FIELDS = (
    "platform",
    "run_id",
    "task_name",
    "current_dt",
    "previous_date",
    "universe",
    "portfolio",
    "timestamp",
)


def make_context(
    platform: str,
    *,
    run_id: str = "",
    task_name: str = "",
    current_dt: datetime | None = None,
    previous_date: date | None = None,
    universe: list[str] | None = None,
    portfolio: Any = None,
    timestamp: datetime | None = None,
    **extras: Any,
) -> SimpleNamespace:
    """构造 context（内核字段就位 + 平台扩展槽透传）。"""
    ctx = SimpleNamespace(
        platform=platform,
        run_id=run_id,
        task_name=task_name,
        current_dt=current_dt,
        previous_date=previous_date,
        universe=list(universe or []),
        portfolio=portfolio,
        timestamp=timestamp if timestamp is not None else current_dt,
    )
    for key, value in extras.items():
        setattr(ctx, key, value)
    return ctx


def refresh_context(
    ctx: SimpleNamespace,
    *,
    current_dt: datetime,
    previous_date: date | None,
    universe: list[str],
    portfolio: Any,
) -> None:
    """每 bar 引擎刷新（4.4: 原位更新, 策略已持有的 context 引用不失效）。"""
    ctx.current_dt = current_dt
    ctx.previous_date = previous_date
    ctx.universe = list(universe)
    ctx.portfolio = portfolio
    ctx.timestamp = current_dt
