# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 12:20:00
# @update_time        : 2026/08/16 21:59:08
# @description : g02 平台版（PTrade）：单次买卖——oracle 与 native g02 同源, 策略 ptrade 化

"""g02 平台版（PTrade, D3）。手算依据与 native 完全一致（模块 docstring 见 native 版）:
day10 买 order_target_value(500,000) → day11 10.01×50,000; day20 全卖 → day21 9.99。
"""

from __future__ import annotations

from datetime import datetime

from mtzquant.engine.orders import OrderDirection, OrderStatus
from tests.golden.conftest import flat_series, load_expected  # noqa: F401
from tests.golden.framework import assert_six

from .bridge import run_ptrade_golden

CODE = "600000.SH"
N = 60
PX = 10.0
QTY = 50_000
FILL_PX_BUY = 10.01
FILL_PX_SELL = 9.99
BUY_AMOUNT = 500_500.0
SELL_AMOUNT = 499_500.0
COMM_BUY = 50.05
COMM_SELL = 49.95
STAMP_SELL = 249.75

PTRADE_G02 = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 10:
        order_target_value('600000.SS', 500000)
    if g.n == 20:
        order_target_value('600000.SS', 0.0)
"""


def test_g02_ptrade_single_buy_sell(gdriver) -> None:
    gdriver.add_data({CODE: flat_series(CODE, N)})
    snap = run_ptrade_golden(gdriver, PTRADE_G02)
    bars = flat_series(CODE, N)
    d11, d21 = bars[10].date, bars[20].date

    assert len(snap.orders) == 2
    assert_six(
        snap,
        orders=[
            {"code": CODE, "side": OrderDirection.BUY, "status": OrderStatus.FILLED, "qty": QTY},
            {"code": CODE, "side": OrderDirection.SELL, "status": OrderStatus.FILLED, "qty": QTY},
        ],
        fills=[
            {
                "code": CODE,
                "side": OrderDirection.BUY,
                "price": FILL_PX_BUY,
                "volume": QTY,
                "amount": BUY_AMOUNT,
                "fill_time": datetime.strptime(f"{d11} 09:30", "%Y-%m-%d %H:%M"),
            },
            {
                "code": CODE,
                "side": OrderDirection.SELL,
                "price": FILL_PX_SELL,
                "volume": QTY,
                "amount": SELL_AMOUNT,
                "fill_time": datetime.strptime(f"{d21} 09:30", "%Y-%m-%d %H:%M"),
            },
        ],
        cash=[
            (
                datetime.strptime(f"{d11} 09:30", "%Y-%m-%d %H:%M"),
                -(BUY_AMOUNT + COMM_BUY),
                f"buy {CODE}",
            ),
            (
                datetime.strptime(f"{d21} 09:30", "%Y-%m-%d %H:%M"),
                SELL_AMOUNT - COMM_SELL - STAMP_SELL,
                f"sell {CODE}",
            ),
        ],
        positions={},
        fees={"commission": 100.0, "stamp_tax": STAMP_SELL, "transfer_fee": 0.0},
        status="completed_exact",
    )
    # NAV oracle（expected/g02.json 复用）
    EXP = load_expected("g02")
    chk = {cp["day"]: cp["nav"] for cp in EXP["checkpoints"]}
    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    assert abs(nav_map[bars[10].date] - chk[11]) <= 1e-10
    assert abs(nav_map[bars[20].date] - chk[21]) <= 1e-10
    assert snap.fee_total == EXP["fees"]["total"]  # 349.75


def test_g02_ptrade_min_commission_boundary(gdriver) -> None:
    """佣金下限边界（ptrade order）: 100 股 × 5.005 → 佣金 5.0。"""
    script = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])

def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order('600000.SS', 100)
"""
    gdriver.add_data({CODE: flat_series(CODE, N, price=5.0)})
    snap = run_ptrade_golden(gdriver, script)
    assert snap.fees["commission"] == 5.0
    assert len(snap.fills) == 1
    assert abs(snap.fills[0].price - 5.005) <= 1e-10
    assert abs(snap.fills[0].amount - 500.5) <= 1e-10
