# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 12:26:00
# @update_time        : 2026/08/16 21:59:08
# @description : g05/g06 平台版（PTrade）：停牌 stale 估值/过期复牌 + 现金不足拒单

"""g05/g06 平台版（PTrade, D3）。手算依据与 native 一致:
- g05: day10 买 10,000 → day11 成交 100,100+佣金 10.01 → 现金 889,889.99;
  day20-22 停牌 stale 估值 NAV=0.98988999 连续; 停牌日挂单 expire。
- g06: 初始 100,000, 目标 500,000 → rejected(insufficient_cash); 对照 49,000 成交。
"""

from __future__ import annotations

from mtzquant.engine.orders import OrderDirection, OrderStatus
from tests.golden.conftest import flat_series, make_bars  # noqa: F401
from tests.golden.framework import assert_six

from .bridge import run_ptrade_golden

CODE = "600000.SH"
PX = 10.0
FILL_PX_BUY = 10.01
NAV_HOLD = 0.99988999  # (1e6 − 100,100 − 10.01 + 10,000×10) / 1e6 手算


def _suspended_bars() -> list:
    return make_bars(CODE, [PX] * 25, suspended={19, 20, 21})  # day20-22 停牌


PTRADE_G05 = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 10:
        order('600000.SS', 10000)
"""


def test_g05_ptrade_suspend_valuation_stale(gdriver) -> None:
    """停牌段估值沿用前收: day19/20/22/23 NAV 全 = 0.98988999。"""
    gdriver.add_data({CODE: _suspended_bars()})
    snap = run_ptrade_golden(gdriver, PTRADE_G05)
    bars = _suspended_bars()

    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    for idx in (18, 19, 21, 22):  # day19/20/22/23
        d = bars[idx].date
        assert abs(nav_map[d] - NAV_HOLD) <= 1e-10, f"{d}: NAV {nav_map[d]}"
    assert nav_map[bars[18].date] == nav_map[bars[19].date] == nav_map[bars[21].date]
    assert len(snap.fills) == 1  # 停牌期间无成交


PTRADE_G05_EXPIRE = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 20:
        order('600000.SS', 10000)
"""


def test_g05_ptrade_suspend_order_expire(gdriver) -> None:
    """停牌日（day20）挂单 → expire + 降级。"""
    gdriver.add_data({CODE: _suspended_bars()})
    snap = run_ptrade_golden(gdriver, PTRADE_G05_EXPIRE)

    assert len(snap.orders) == 1
    assert snap.orders[0].status is OrderStatus.EXPIRED
    assert snap.fills == []
    assert snap.status == "completed_degraded"


PTRADE_G06_BIG = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order_target_value('600000.SS', 500000)
"""


def test_g06_ptrade_insufficient_cash_reject(gdriver, monkeypatch) -> None:
    """初始 100,000, 目标 500,000 → rejected(insufficient_cash), 无冻结残留。"""
    gdriver.account.available_cash = 100_000.0  # gdriver 默认 1e6, 本例 1e5
    gdriver.add_data({CODE: flat_series(CODE, 10)})
    snap = run_ptrade_golden(gdriver, PTRADE_G06_BIG)

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


PTRADE_G06_OK = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order_target_value('600000.SS', 49000)
"""


def test_g06_ptrade_within_budget_succeeds(gdriver) -> None:
    """对照: 目标 49,000 → 4,900 股成交（10.01×4,900=49,049）。"""
    gdriver.account.available_cash = 100_000.0
    gdriver.add_data({CODE: flat_series(CODE, 10)})
    snap = run_ptrade_golden(gdriver, PTRADE_G06_OK)

    assert len(snap.fills) == 1
    assert abs(snap.fills[0].amount - round(FILL_PX_BUY * 4_900, 2)) <= 1e-6
    assert snap.fees["commission"] > 0.0
    assert snap.status == "completed_exact"
