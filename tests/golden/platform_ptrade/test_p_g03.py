# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 12:24:00
# @update_time        : 2026/08/16 21:59:08
# @description : g03/g04 平台版（PTrade）：T+1 当日卖拒 + 一字板过期——oracle 同源

"""g03/g04 平台版（PTrade, D3）。手算依据与 native 版一致:
- g03: day10 买(50,000@10.01 次日) → day11 当日卖 rejected(t_plus) → day12 卖成功(9.99 次日)
- g04: day14 挂大额买 → day15 一字涨停 expire（one_word_limit 标记 + degradation）
"""

from __future__ import annotations

from dataclasses import replace

from mtzquant.engine.orders import OrderDirection, OrderStatus
from tests.golden.conftest import flat_series, load_expected, make_bars  # noqa: F401
from tests.golden.framework import assert_six

from .bridge import run_ptrade_golden

CODE = "600000.SH"
PX = 10.0
QTY = 50_000
FILL_PX_BUY = 10.01
FILL_PX_SELL = 9.99

PTRADE_G03 = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 10:
        order('600000.SS', 50000)
    if g.n == 11:
        order('600000.SS', -50000)
    if g.n == 12:
        order('600000.SS', -50000)
"""


def test_g03_ptrade_t_plus_sell_rejected_then_ok(gdriver) -> None:
    """当日买→当日卖(rejected)→次日卖(filled), 三单两成交。"""
    from datetime import datetime

    gdriver.add_data({CODE: flat_series(CODE, 30)})
    snap = run_ptrade_golden(gdriver, PTRADE_G03)
    bars = flat_series(CODE, 30)
    d11, d13 = bars[10].date, bars[12].date

    assert len(snap.orders) == 3
    assert_six(
        snap,
        orders=[
            {"code": CODE, "side": OrderDirection.BUY, "status": OrderStatus.FILLED, "qty": QTY},
            {"code": CODE, "side": OrderDirection.SELL, "status": OrderStatus.REJECTED, "qty": QTY},
            {"code": CODE, "side": OrderDirection.SELL, "status": OrderStatus.FILLED, "qty": QTY},
        ],
        fills=[
            {
                "code": CODE,
                "side": OrderDirection.BUY,
                "price": FILL_PX_BUY,
                "volume": QTY,
                "amount": round(FILL_PX_BUY * QTY, 2),
                "fill_time": datetime.strptime(f"{d11} 09:30", "%Y-%m-%d %H:%M"),
            },
            {
                "code": CODE,
                "side": OrderDirection.SELL,
                "price": FILL_PX_SELL,
                "volume": QTY,
                "amount": round(FILL_PX_SELL * QTY, 2),
                "fill_time": datetime.strptime(f"{d13} 09:30", "%Y-%m-%d %H:%M"),
            },
        ],
        positions={},
        status="completed_exact",
    )
    rejected_evts = [e for e in snap.order_events if e.event_type.value == "rejected"]
    assert len(rejected_evts) == 1
    assert rejected_evts[0].info_json == {"reason": "t_plus_sell_unavailable"}


def _limit_bars() -> list:
    bars = make_bars(CODE, [PX] * 20)
    bars[14] = replace(bars[14], limit_up=True)  # day15 一字涨停
    return bars


PTRADE_G04 = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 14:
        order('600000.SS', 50000)
"""


def test_g04_ptrade_one_word_board_expire(gdriver) -> None:
    """一字涨停整日不成交 → expire + one_word_limit 标记 + 降级。"""
    gdriver.add_data({CODE: _limit_bars()})
    snap = run_ptrade_golden(gdriver, PTRADE_G04)

    assert_six(
        snap,
        orders=[
            {"code": CODE, "side": OrderDirection.BUY, "status": OrderStatus.EXPIRED, "qty": QTY},
        ],
        fills=[],
        positions={},
        fees={"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0},
        status="completed_degraded",
    )
    assert snap.cash.available == []  # 无冻结泄漏
    expire_evts = [e for e in snap.order_events if e.event_type.value == "expire"]
    assert len(expire_evts) == 1
    assert expire_evts[0].info_json is not None
    assert expire_evts[0].info_json.get("one_word_limit") is True
    # NAV oracle（expected/g04.json 复用）: 一字板日无成交 → NAV 恒 1.0
    EXP = load_expected("g04")
    bars = _limit_bars()
    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    for cp in EXP["checkpoints_primary"]:
        d = bars[cp["day"] - 1].date
        assert abs(nav_map[d] - cp["nav"]) <= 1e-10
    assert snap.fee_total == EXP["fees"]["total"] == 0.0
