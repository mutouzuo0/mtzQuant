# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:45:00
# @description : g07 最低佣金触发：小额 500 元 → 佣金 5.0；对照大额 20 万 → 20.0

"""黄金用例 g07：最低佣金触发（测试方案 §5 g07）。

场景：100 股 × 5.0 = 500 元成交额（小额定投）→ commission=max(5,0.05)=5.0；
对照：200,000 元成交额 → commission=0.0001×200,000=20.0（字面量断言）。
"""

from __future__ import annotations

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import flat_series, load_expected
from .daily import DailyDriver
from .framework import MockBroker, assert_six

CODE_LO = "510300.SH"
CODE_HI = "159915.SZ"
N = 10

EXP = load_expected("g07")  # §2.4 oracle：expected/g07.json 手算期望值


def test_g07_min_commission_floor() -> None:
    """500 元成交额 → 佣金恰为下限 5.0（而非 0.05）。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=100_000.0)
    bars = flat_series(CODE_LO, N, price=5.0)  # 100 股×5.0=500
    driver.add_data({CODE_LO: bars})
    d0 = bars[0].date

    def _small_buy() -> None:
        o = driver.order(CODE_LO, OrderDirection.BUY, 100)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "fill", "price": 5.0})

    driver.on(d0, _small_buy)
    snap = driver.run()

    # oracle 手算：commission=5.0、NAV=0.99995、fee_total=5.0
    low = EXP["low"]
    assert snap.fees["commission"] == low["commission"]
    assert_six(
        snap,
        fills=[
            {
                "code": CODE_LO,
                "side": OrderDirection.BUY,
                "price": 5.005,  # ask 侧 5.0×1.001（真实撮合, 5.3.3）
                "volume": 100.0,
                "amount": 500.5,
            },
        ],
        fees={"commission": low["commission"], "stamp_tax": 0.0, "transfer_fee": 0.0},
        status="completed_exact",
    )
    assert snap.fee_total == EXP["fees_low"]["total"]
    # day0 未成交 NAV=1.0；day1 起成交后 NAV=oracle 手算 0.999945（恒）
    navs = [p.nav for p in snap.nav_series]
    assert abs(navs[0] - 1.0) <= 1e-10
    assert all(abs(n - low["nav"]) <= 1e-10 for n in navs[1:])


def test_g07_large_commission_proportional() -> None:
    """对照：200,200 元成交额 → 佣金=20.02（比例段，未触下限）。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = flat_series(CODE_HI, N, price=10.0)
    driver.add_data({CODE_HI: bars})
    d0 = bars[0].date
    qty = 20_000  # 20,000×10.01=200,200

    def _big_buy() -> None:
        o = driver.order(CODE_HI, OrderDirection.BUY, qty)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "fill", "price": 10.0})

    driver.on(d0, _big_buy)
    snap = driver.run()

    high = EXP["high"]
    assert snap.fees["commission"] == high["commission"]  # oracle：20.02（0.0001×200,200）
    assert snap.fee_total == EXP["fees_high"]["total"]
    assert len(snap.fills) == 1
    assert abs(snap.fills[0].price - 10.01) <= 1e-10
    navs = [p.nav for p in snap.nav_series]
    assert abs(navs[0] - 1.0) <= 1e-10  # day0 未成交
    assert all(abs(n - high["nav"]) <= 1e-10 for n in navs[1:])
