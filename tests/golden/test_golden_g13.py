# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/16 00:50:00
# @description : g13 分钟/日线边界（日线侧）：秒级时间戳链 + 分钟侧 M5 挂起

"""黄金用例 g13：分钟/日线边界（测试方案 §5 g13）。

日线侧（本文件即测）：day t 15:00 下单 → eligible_fill_at=t+1 09:30 →
成交时间戳=次日 09:30（非 t 日 15:00）；日线 bar 时间戳=15:00；订单/成交
回测内时刻逐字段精确断言。

分钟侧（盘中定时调度精确性、分钟 bar 推进）标记 m5_deferred 跳过，
skip 输出注明 M5 补齐——阶段 F 起由真实分钟引擎接管。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import flat_series, make_bars
from .daily import DailyDriver
from .framework import MockBroker, assert_six

CODE = "600111.SH"
N = 12
PX = 10.0


def _build() -> tuple[DailyDriver, list]:
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = flat_series(CODE, N, price=PX)
    driver.add_data({CODE: bars})
    return driver, bars


def test_g13_daily_side_timestamps() -> None:
    """日线全链路时刻断言：bar=15:00、下单=15:00、撮合=次日 09:30。"""
    driver, bars = _build()
    t0, t1 = bars[5].date, bars[6].date

    record: dict = {}

    def _buy_day5() -> None:
        o = driver.order_target_value(CODE, 100_000.0)  # → 10,000 股 @10.0
        assert o is not None and o.status is OrderStatus.PENDING
        record["oid"] = o.order_id
        record["submitted_at"] = o.submitted_at
        record["eligible"] = o.eligible_fill_at

    driver.on(t0, _buy_day5)
    snap = driver.run()

    # ---- 日线 bar 时间戳 = 15:00（4.7）----
    assert bars[5].dt == datetime.strptime(f"{t0} 15:00", "%Y-%m-%d %H:%M")

    # ---- 下单时刻 = t0 15:00；撮合时刻 = t1 09:30（非 t0 15:00）----
    when_sub = datetime.strptime(f"{t0} 15:00", "%Y-%m-%d %H:%M")
    when_open = datetime.strptime(f"{t1} 09:30", "%Y-%m-%d %H:%M")
    assert record["submitted_at"] == when_sub
    assert record["eligible"] == when_open

    o = next(x for x in snap.orders if x.order_id == record["oid"])
    assert o.eligible_fill_at == when_open
    assert o.status is OrderStatus.FILLED

    assert_six(
        snap,
        orders=[
            {"code": CODE, "side": OrderDirection.BUY, "status": OrderStatus.FILLED, "qty": 10_000}
        ],
        fills=[
            {
                "code": CODE,
                "side": OrderDirection.BUY,
                "price": round(PX * 1.001, 4),  # 10.01（真实撮合 ask 侧滑点, 5.3.3）
                "volume": 10_000,
                "amount": round(PX * 1.001, 4) * 10_000,
                "fill_time": when_open,
            },
        ],
    )

    # ---- 事件时间戳链：ACCEPTED@15:00 → FILL@次日 09:30 ----
    evs = {e.event_type.name: e for e in snap.order_events}
    assert evs["ACCEPTED"].event_time == when_sub
    assert evs["FILL"].event_time == when_open

    # ---- 日线净值点时刻 = 15:00（估值基准）----
    nav_dt = {p.dt.date().isoformat(): p.dt for p in snap.nav_series}
    assert nav_dt[t1] == datetime.strptime(f"{t1} 15:00", "%Y-%m-%d %H:%M")


def test_g13_daily_afterclose_trade_next_open() -> None:
    """盘后调度窗内下单仍是已收盘日委托：成交时刻=次日开盘，不是当日 15:00。"""
    driver, bars = _build()
    t0, t1 = bars[3].date, bars[4].date

    def _after_close_d3() -> None:
        o = driver.order(CODE, OrderDirection.BUY, 2_000)
        assert o is not None and o.status is OrderStatus.PENDING
        driver.broker.program(o.order_id, {"type": "fill", "price": PX})

    driver.on_after_close(t0, _after_close_d3)
    snap = driver.run()
    assert len(snap.fills) == 1
    f = snap.fills[0]
    assert f.fill_time == datetime.strptime(f"{t1} 09:30", "%Y-%m-%d %H:%M")


@pytest.mark.m5_deferred
@pytest.mark.skip(
    reason=(
        "M5 补齐：分钟侧语义（盘中定时调度精确性、分钟 bar 推进）由 M5 引擎实现，阶段 C/M1 不验收"
    )
)
def test_g13_minute_side_timed_scheduling() -> None:
    """（M5 占位）分钟侧：分钟 bar 推进 + 定时调度点（9:31/10:30/14:57…）精确性。"""
    make_bars(CODE, [PX] * 5)  # 构造调用仅供占位：断言体随 M5 引擎实现
    raise AssertionError("M5 引擎落地后实现分钟侧断言")
