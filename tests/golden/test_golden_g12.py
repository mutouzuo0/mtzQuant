# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/16 00:35:00
# @description : g12 退市标的（终止估值）：冻结估值 + 退市后拒单 + open/stale 标记

"""黄金用例 g12：退市标的终止估值（测试方案 §5 g12）。

构造：标的 A（健康，50 日，价 10.0 平坦）+ 标的 C（退市：40 日后无 bar，
delist_date=第 40 日即最后交易日）。初始持仓 A=10,000 股、C=10,000 股（成本=市价）。
- day40 后 C 不再参与撮合/下单：退市后新单 → rejected(suspended/delisted)；
- C 估值冻结于最后有效收盘（8.0）并逐日标记 stale；open_positions 退市后 2→1；
- NAV 冻结期连续无 NaN；A 正常交易估值不受影响。

手算 NAV（1e-10，day40 前 / 冻结期）：
- day39（最后交易日）：cash 1,000,000 + A 10,000×10.0 + C 10,000×8.0
  = 1,000,000 + 100,000 + 80,000 = 1,180,000 → nav=1.18
- day44（退市冻结期）：A 10,000×10.0 = 100,000；C 冻结 10,000×8.0 = 80,000
  同 1,180,000 → nav=1.18（冻结：C 无当日 bar 但沿用最后有效收盘）
"""

from __future__ import annotations

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import make_bars
from .daily import DailyDriver
from .framework import MockBroker, assert_six

CODE_A = "600001.SH"
CODE_C = "600002.SH"  # 退市标的
N = 50
DELIST_AT = 39  # C 最后交易日（0-based 索引；plan 的 day40）
PX_A = 10.0
PX_C = 8.0
QTY = 10_000


def _build() -> DailyDriver:
    broker = MockBroker()
    bars_a = make_bars(CODE_A, [PX_A] * N, start="2026-01-05")
    bars_c = make_bars(CODE_C, [PX_C] * (DELIST_AT + 1), start="2026-01-05")  # 仅 40 根 bar
    driver = DailyDriver(
        broker,
        initial_cash=1_000_000.0,
        initial_positions={CODE_A: QTY, CODE_C: QTY},
    )
    driver.add_data({CODE_A: bars_a, CODE_C: bars_c})
    driver.on_delist(CODE_C, bars_c[DELIST_AT].date)
    return driver


def test_g12_delist_frozen_valuation() -> None:
    """退市后估值冻结+stale 标记、open_positions 2→1、NAV 连续无 NaN。"""
    driver = _build()
    delist_day = list(driver.data[CODE_C])[DELIST_AT]  # 字符串日期（最后一个 C bar）
    session_dates = [s.bar.date for s in driver.sessions]
    d39 = delist_day
    d44 = session_dates[44]

    snap = driver.run()

    nav_map = {
        p.dt.date().isoformat(): (p.nav, p.equity, p.stale_codes, p.open_positions)
        for p in snap.nav_series
    }

    # ---- day39（最后交易日）：当日 C 仍正常估值，无 stale ----
    prev, eq, stale, openp = nav_map[d39]
    assert abs(prev - 1.18) <= 1e-10, f"day39 nav {prev} != 1.18"
    assert abs(eq - 1_180_000.0) <= 1e-6
    assert stale == (), f"day39 不应有 stale: {stale}"
    assert openp == 2

    # ---- day44（冻结期）：C 冻结 8.0 标记 stale，A 正常；open_positions→1 ----
    nav44, eq44, stale44, open44 = nav_map[d44]
    assert abs(nav44 - 1.18) <= 1e-10, f"day44 nav {nav44} != 1.18"
    assert abs(eq44 - 1_180_000.0) <= 1e-6
    assert stale44 == (CODE_C,)
    assert open44 == 1

    # ---- metrics 无 NaN：全序列 nav 有限 ----
    import math

    for p in snap.nav_series:
        assert math.isfinite(p.nav), f"{p.dt}: nav {p.nav} 非有限"
        assert math.isfinite(p.equity), f"{p.dt}: equity {p.equity} 非有限"

    # ---- 末态持仓：C 数量不变、估值为冻结价 ----
    assert_six(
        snap,
        positions={
            CODE_A: {
                "total_qty": QTY,
                "avg_cost": PX_A,
                "last_price": PX_A,
                "market_value": PX_A * QTY,
            },
            CODE_C: {
                "total_qty": QTY,
                "avg_cost": PX_C,
                "last_price": PX_C,
                "market_value": PX_C * QTY,
            },
        },
        status="completed_exact",
    )


def test_g12_delist_orders_rejected() -> None:
    """退市后新单 rejected（suspended/delisted）；退市当日仍可正常交易。"""
    driver = _build()
    session_dates = [s.bar.date for s in driver.sessions]
    d40_after = session_dates[DELIST_AT + 1]  # 退市后首个交易日

    def _after() -> None:
        o_c = driver.order(CODE_C, OrderDirection.BUY, 100)  # 退市后下单 → 拒
        assert o_c.status is OrderStatus.REJECTED, f"退市后应拒单，实际 {o_c.status}"
        assert o_c.reject_reason == "suspended/delisted"
        o_a = driver.order(CODE_A, OrderDirection.BUY, 100)  # 健康标的正常受理
        assert o_a.status is OrderStatus.PENDING
        driver.broker.program(o_a.order_id, {"type": "fill", "price": PX_A})

    driver.on(d40_after, _after)
    snap = driver.run()

    # day40(退市后)：C 单拒绝（无成交），A 单次日成交
    assert any(ev.event_type.name == "REJECTED" for ev in snap.order_events)
    assert len(snap.fills) == 1
    assert len(snap.orders) == 2
    by_code = {o.code: o for o in snap.orders}
    assert by_code[CODE_C].status is OrderStatus.REJECTED
    assert by_code[CODE_A].status is OrderStatus.FILLED
    # 运行仍正常 completed（拒单不构成降级）
    assert snap.status == "completed_exact"
