# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:50:00
# @description : g08 order_target_value 归一：数量差→整手取整边界 + 零差忽略 + 方向判定

"""黄金用例 g08：order_target_value 归一（测试方案 §5 g08）。

场景：持仓 10,000 股（价 10.0，市值 100,000），初始现金充足；
- 目标 135,000 → 数量差 35,000 → 3,500 股（整百）→ BUY
- 目标 120,400 → 数量差 20,400 → 整百取整 2,000 股 → BUY（2,040→floor 2,000）
- 目标 96,300 → 数量差 −3,700 → −370→ 整百 300 股 → SELL（差额 3,700→370 整手取 300）
- 目标 100,000 → 差=0 → 忽略（无订单行、无事件）

断言：归一后 qty、方向、事件数；整手取整边界 3,450→3,400 明文验证。
"""

from __future__ import annotations

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import flat_series
from .daily import DailyDriver
from .framework import MockBroker

CODE = "600000.SH"
N = 10
PX = 10.0
HELD = 10_000
SLIP = 0.001
FILL_PX = round(PX * (1.0 + SLIP), 4)


def _build() -> tuple[DailyDriver, list]:
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = flat_series(CODE, N)
    driver.add_data({CODE: bars})
    # 初始持仓 10,000 股 @ 10.0（市价 100,000）
    from mtzquant.engine.account import Position

    driver.account.positions[CODE] = Position(code=CODE, total_qty=HELD, avg_cost=PX, last_price=PX)
    return driver, bars


def test_g08_target_value_normalize_boundary() -> None:
    """目标 135,000 → 买 3,500；120,400 → 买 2,000（floor 边界）。"""
    driver, bars = _build()
    d0 = bars[0].date
    orders: list = []

    def _targets() -> None:
        o1 = driver.order_target_value(CODE, 135_000.0)
        assert o1.status is OrderStatus.PENDING
        assert o1.qty == 3_500, o1.qty
        orders.append(o1)
        o2 = driver.order_target_value(CODE, 120_400.0)
        assert o2.status is OrderStatus.PENDING
        # 数量差 20,400/10=2,040 → floor 2,000（整百）
        assert o2.qty == 2_000, o2.qty
        orders.append(o2)

    driver.on(d0, _targets)
    snap = driver.run()

    # 三单皆 PENDING 未成交（未编程 broker）→ 融资检查只走受理面
    assert [o.side for o in snap.orders] == [OrderDirection.BUY, OrderDirection.BUY]
    assert [o.qty for o in snap.orders] == [3_500, 2_000]


def test_g08_target_value_zero_diff_ignored() -> None:
    """差=0 忽略：目标=市价 100,000 → 无订单行、无事件。"""
    driver, bars = _build()
    d0 = bars[0].date
    before_orders = driver.order_count
    before_events = driver.event_count

    def _noop_target() -> None:
        driver.order_target_value(CODE, 100_000.0)

    driver.on(d0, _noop_target)
    snap = driver.run()
    assert len(snap.orders) == before_orders
    assert len(snap.order_events) == before_events


def test_g08_target_value_sell_direction() -> None:
    """目标低于市值 → SELL 方向；整手边界 3,450→3,400 顺带覆盖。"""
    driver, bars = _build()
    d0 = bars[0].date

    def _target_below() -> None:
        o = driver.order_target_value(CODE, 96_300.0)  # 差 −3,700/10=370 → floor 300
        assert o.status is OrderStatus.PENDING
        assert o.qty == 300, o.qty
        # 边界：目标 134,500 → 差 +34,500/10=3,450 → floor 3,400
        o2 = driver.order_target_value(CODE, 134_500.0)
        assert o2.qty == 3_400, o2.qty

    driver.on(d0, _target_below)
    snap = driver.run()
    assert snap.orders[0].side is OrderDirection.SELL
    assert snap.orders[0].qty == 300
    assert snap.orders[1].side is OrderDirection.BUY
    assert snap.orders[1].qty == 3_400


def test_g08_lot_floor_boundary_3450() -> None:
    """整手取整边界：差额 34,500 → 3,450 股 → floor 3,400（4.5 归一）。"""
    from mtzquant.engine.account import Position

    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = flat_series(CODE, N)
    driver.add_data({CODE: bars})
    driver.account.positions[CODE] = Position(code=CODE, total_qty=HELD, avg_cost=PX, last_price=PX)
    d0 = bars[0].date

    def _target() -> None:
        o = driver.order_target_value(CODE, 134_500.0)  # 差 34,500/10=3,450 → 3,400
        assert o.qty == 3_400, o.qty

    driver.on(d0, _target)
    driver.run()
