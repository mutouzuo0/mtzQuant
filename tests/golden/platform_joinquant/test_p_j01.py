# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 13:26:00
# @update_time        : 2026/08/16 21:59:08
# @description : g01/g02 平台版（聚宽）：空仓 + 单次买卖——oracle 与 native 同源, 策略聚宽化

"""g01/g02 平台版（聚宽, D3）。手算依据与 native 完全一致（见 native 版 docstring）。
聚宽脚本写法: set_universe(['600000.XSHG']) + handle_data(context,data) + order_target_value。
"""

from __future__ import annotations

from datetime import datetime

from mtzquant.engine.orders import OrderDirection, OrderStatus
from tests.golden.conftest import flat_series, load_expected  # noqa: F401
from tests.golden.framework import assert_six

from .bridge import run_joinquant_golden

N_DAYS = 250
CODES = ("600000.SH", "510300.SH", "159915.SZ")

JQ_G01 = """\
def initialize(context):
    set_universe(['600000.XSHG', '510300.XSHG', '159915.XSHE'])


def handle_data(context, data):
    pass
"""


def test_g01_jq_empty_strategy(gdriver) -> None:
    """3×250 空间曲线（聚宽）: 无订单无成交无费用, nav 全 1.0。"""
    gdriver.add_data({c: flat_series(c, N_DAYS) for c in CODES})
    snap = run_joinquant_golden(gdriver, JQ_G01)

    assert len(snap.nav_series) == N_DAYS
    expected_nav = [{"dt": p.dt, "nav": 1.0, "equity": 1_000_000.0} for p in snap.nav_series]
    assert_six(
        snap,
        orders=[],
        fills=[],
        cash=[],
        positions={},
        fees={"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total": 0.0},
        nav=expected_nav,
        status="completed_exact",
    )
    assert gdriver.account.available_cash == 1_000_000.0


# ------------------------------------------------------------------
# g02 单次买卖（oracle: expected/g02.json, 字面量手算）
# ------------------------------------------------------------------
CODE = "600000.SH"
N = 60
QTY = 50_000
COMM_BUY = 50.05
COMM_SELL = 49.95
STAMP_SELL = 249.75

JQ_G02 = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.XSHG'])


def handle_data(context, data):
    g.n += 1
    if g.n == 10:
        order_target_value('600000.XSHG', 500000)
    if g.n == 20:
        order_target_value('600000.XSHG', 0.0)
"""


def test_g02_jq_single_buy_sell(gdriver) -> None:
    gdriver.add_data({CODE: flat_series(CODE, N)})
    snap = run_joinquant_golden(gdriver, JQ_G02)
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
                "price": 10.01,
                "volume": QTY,
                "amount": 500_500.0,
                "fill_time": datetime.strptime(f"{d11} 09:30", "%Y-%m-%d %H:%M"),
            },
            {
                "code": CODE,
                "side": OrderDirection.SELL,
                "price": 9.99,
                "volume": QTY,
                "amount": 499_500.0,
                "fill_time": datetime.strptime(f"{d21} 09:30", "%Y-%m-%d %H:%M"),
            },
        ],
        cash=[
            (
                datetime.strptime(f"{d11} 09:30", "%Y-%m-%d %H:%M"),
                -(500_500.0 + COMM_BUY),
                f"buy {CODE}",
            ),
            (
                datetime.strptime(f"{d21} 09:30", "%Y-%m-%d %H:%M"),
                499_500.0 - COMM_SELL - STAMP_SELL,
                f"sell {CODE}",
            ),
        ],
        positions={},
        fees={"commission": 100.0, "stamp_tax": STAMP_SELL, "transfer_fee": 0.0},
        status="completed_exact",
    )
    EXP = load_expected("g02")
    chk = {cp["day"]: cp["nav"] for cp in EXP["checkpoints"]}
    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    assert abs(nav_map[bars[10].date] - chk[11]) <= 1e-10
    assert abs(nav_map[bars[20].date] - chk[21]) <= 1e-10
    assert snap.fee_total == EXP["fees"]["total"]  # 349.75


JQ_G02_MINCOMM = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.XSHG'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order('600000.XSHG', 100)
"""


def test_g02_jq_min_commission_boundary(gdriver) -> None:
    """佣金下限边界: 100 股 × 5.005 → 佣金 5.0。"""
    gdriver.add_data({CODE: flat_series(CODE, N, price=5.0)})
    snap = run_joinquant_golden(gdriver, JQ_G02_MINCOMM)
    assert snap.fees["commission"] == 5.0
    assert len(snap.fills) == 1
    assert abs(snap.fills[0].price - 5.005) <= 1e-10
