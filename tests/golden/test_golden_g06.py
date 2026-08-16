# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:40:00
# @description : g06 现金不足下单：insufficient_cash 拒单 + 金额验证 + 预算内对照成功

"""黄金用例 g06：现金不足下单（测试方案 §5 g06）。

场景：初始资金 100,000；day0 15:00 目标买入 500,000（超可用）→ 受理前拒单；
对照组：目标 49,000（预算内）→ 正常成交。

断言：
- rejected（reason=insufficient_cash），order_events 含 reason；
- 无成交、无冻结残留（现金流水空）；
- 对照单成交正常（fills=1，金额≈49,000）。
"""

from __future__ import annotations

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import flat_series
from .daily import DailyDriver
from .framework import MockBroker, assert_six

CODE = "600000.SH"
N = 10
PX = 10.0
SLIP = 0.001
FILL_PX = round(PX * (1.0 + SLIP), 4)  # 10.01


def test_g06_insufficient_cash_reject() -> None:
    """目标 500,000 > 可用 100,000 → rejected，无成交无冻结残留。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=100_000.0)
    bars = flat_series(CODE, N)
    driver.add_data({CODE: bars})
    d0 = bars[0].date

    def _order_too_big() -> None:
        o = driver.order_target_value(CODE, 500_000.0)
        assert o.status is OrderStatus.REJECTED
        assert o.reject_reason == "insufficient_cash"

    driver.on(d0, _order_too_big)
    snap = driver.run()

    assert_six(
        snap,
        orders=[
            {
                "code": CODE,
                "side": OrderDirection.BUY,
                "status": OrderStatus.REJECTED,
                "qty": 50_000,
            },
        ],
        fills=[],
        cash=[],
        positions={},
        fees={"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0},
        status="completed_exact",
    )
    rej = [e for e in snap.order_events if e.event_type.value == "rejected"]
    assert len(rej) == 1
    assert rej[0].info_json == {"reason": "insufficient_cash"}


def test_g06_within_budget_succeeds() -> None:
    """对照：目标 49,000（预算内）→ accepted → 成交。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=100_000.0)
    bars = flat_series(CODE, N)
    driver.add_data({CODE: bars})
    d0 = bars[0].date

    def _order_ok() -> None:
        o = driver.order_target_value(CODE, 49_000.0)
        assert o.status is OrderStatus.PENDING
        assert o.qty == 4_900  # 49,000/10 → 4,900 股（整百）
        broker.program(o.order_id, {"type": "fill", "price": FILL_PX})

    driver.on(d0, _order_ok)
    snap = driver.run()

    assert len(snap.fills) == 1
    assert abs(snap.fills[0].amount - round(FILL_PX * 4_900, 2)) <= 1e-6
    assert snap.fees["commission"] > 0.0
    assert snap.status == "completed_exact"
