# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:35:00
# @description : g05 停牌跨日持仓估值：stale_price 沿用 + 停牌日挂单 expire + 复牌恢复

"""黄金用例 g05：停牌跨日持仓估值（测试方案 §5 g05）。

场景：单标的平坦 10.0；day10 买入 10,000 → day11 成交；day20-22 停牌
（suspended=True，价格列沿用前收）；day23 复牌正常。

断言：
- 停牌日每日估值沿用 last 有效收盘（10.0）→ NAV 连续无跳变（=day19 收盘基准）；
- 停牌跨段持仓市值按 stale_price 计价且记录 degradation（stale 标记）；
- 停牌日新挂（day 单）→ expire + degradation；
- 复牌日（day23）估值恢复，订单可正常成交（对照组）。
"""

from __future__ import annotations

from datetime import datetime

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import make_bars
from .daily import DailyDriver
from .framework import MockBroker, assert_six

CODE = "600000.SH"
N = 25
PX = 10.0
QTY = 10_000
SLIP = 0.001
FILL_PX = round(PX * (1.0 + SLIP), 4)  # 10.01
BUY_AMOUNT = round(FILL_PX * QTY, 2)  # 100,100.0
COMM = round(max(5.0, 0.0001 * BUY_AMOUNT), 2)  # 10.01
CASH_AFTER = round(1_000_000 - BUY_AMOUNT - COMM, 2)  # 889,889.99
NAV_HOLD = round((CASH_AFTER + PX * QTY) / 1_000_000, 10)  # 0.98988999


def _bars_suspended() -> list:
    """day20-22 停牌（index 19-21），价格 10.0 全平坦。"""
    return make_bars(CODE, [PX] * N, suspended={19, 20, 21})


def _build(broker: MockBroker) -> tuple[DailyDriver, list]:
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = _bars_suspended()
    driver.add_data({CODE: bars})
    return driver, bars


def test_g05_suspend_valuation_stale() -> None:
    """停牌段估值沿用前收：day19/20/21/22 NAV 全部一致。"""
    broker = MockBroker()
    driver, bars = _build(broker)

    d10, d19, d20, d22, d23 = (
        bars[9].date,
        bars[18].date,
        bars[19].date,
        bars[21].date,
        bars[22].date,
    )

    def _buy_day10() -> None:
        o = driver.order(CODE, OrderDirection.BUY, QTY)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "fill", "price": FILL_PX})

    driver.on(d10, _buy_day10)
    snap = driver.run()

    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    for d in (d19, d20, d22):
        assert abs(nav_map[d] - NAV_HOLD) <= 1e-10, f"{d}: NAV {nav_map[d]} != {NAV_HOLD}"
    assert abs(nav_map[d23] - NAV_HOLD) <= 1e-10  # 复牌价同 10.0

    # 停牌段无缝估值：NAV 连续（前后三日的相邻差为零）
    assert nav_map[d19] == nav_map[d20] == nav_map[d22]
    # 停牌期间未发生成交且无收入/冻结泄漏
    assert len(snap.fills) == 1


def test_g05_suspend_order_expire_and_resume() -> None:
    """停牌日挂单 expire+degradation；复牌日新单正常成交。"""
    broker = MockBroker()
    driver, bars = _build(broker)

    d19 = bars[18].date
    d23 = bars[22].date
    d24 = bars[23].date

    def _buy_day19() -> None:
        o = driver.order(CODE, OrderDirection.BUY, QTY)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "expire", "info": "suspended"})  # 停牌日无法成交

    def _buy_day23() -> None:
        o = driver.order(CODE, OrderDirection.BUY, QTY)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "fill", "price": FILL_PX})

    driver.on(d19, _buy_day19)  # 次日 d20 停牌 → expire
    driver.on(d23, _buy_day23)  # 复牌后首日挂单 → d24 成交
    snap = driver.run()

    assert_six(
        snap,
        orders=[
            {"code": CODE, "side": OrderDirection.BUY, "status": OrderStatus.EXPIRED, "qty": QTY},
            {"code": CODE, "side": OrderDirection.BUY, "status": OrderStatus.FILLED, "qty": QTY},
        ],
        fills=[
            {
                "code": CODE,
                "side": OrderDirection.BUY,
                "price": FILL_PX,
                "volume": QTY,
                "amount": BUY_AMOUNT,
                "fill_time": datetime.strptime(f"{d24} 09:30", "%Y-%m-%d %H:%M"),
            },
        ],
        positions={
            CODE: {
                "total_qty": QTY,
                "avg_cost": FILL_PX,
                "last_price": PX,
                "market_value": PX * QTY,
            },
        },
        status="completed_degraded",
    )
    assert any("expired" in d for d in snap.degradations)
