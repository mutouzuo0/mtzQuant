# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 12:30:00
# @update_time        : 2026/08/16 21:59:08
# @description : g07/g08 平台版（PTrade）：佣金边界 + target_value 整手归一/零差忽略/方向

"""g07/g08 平台版（PTrade, D3）。手算依据与 native 一致:
- g07: 小额佣金下限（5.0）/大额比例佣金; g08: 持仓 10,000@10 → target 归一整手边界。
"""

from __future__ import annotations

from mtzquant.engine.account import Position
from mtzquant.engine.orders import OrderDirection
from tests.golden.conftest import flat_series  # noqa: F401

from .bridge import run_ptrade_golden

CODE = "600000.SH"
PX = 10.0
HELD = 10_000
FILL_PX_BUY = 10.01

PTRADE_G07_SMALL = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order('600000.SS', 100)
"""


def test_g07_ptrade_min_commission_floor(gdriver) -> None:
    """小额 100 股 × 5.005=500.5 → 佣金=5.0（下限生效）。"""
    gdriver.add_data({CODE: flat_series(CODE, 10, price=5.0)})
    snap = run_ptrade_golden(gdriver, PTRADE_G07_SMALL)
    assert snap.fees["commission"] == 5.0
    assert snap.fees["stamp_tax"] == 0.0


PTRADE_G07_LARGE = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order('600000.SS', 50000)
"""


def test_g07_ptrade_large_commission_proportional(gdriver) -> None:
    """大额 50,000 股 × 10.01=500,500 → 佣金=50.05（比例档）。"""
    gdriver.add_data({CODE: flat_series(CODE, 10)})
    snap = run_ptrade_golden(gdriver, PTRADE_G07_LARGE)
    assert abs(snap.fees["commission"] - 50.05) <= 1e-10


def _with_position(gdriver) -> None:
    gdriver.add_data({CODE: flat_series(CODE, 10)})
    gdriver.account.positions[CODE] = Position(
        code=CODE, total_qty=HELD, avg_cost=PX, last_price=PX
    )


PTRADE_G08_BUY = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order_target_value('600000.SS', 135000)
        order_target_value('600000.SS', 120400)
"""


def test_g08_ptrade_target_normalize_boundary(gdriver) -> None:
    """135,000 → 买 3,500; 120,400 → 买 2,000（2,040 floor 整百）。"""
    _with_position(gdriver)
    snap = run_ptrade_golden(gdriver, PTRADE_G08_BUY)
    assert [o.side for o in snap.orders] == [OrderDirection.BUY, OrderDirection.BUY]
    assert [o.qty for o in snap.orders] == [3_500, 2_000]


PTRADE_G08_NOOP = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order_target_value('600000.SS', 100000)
"""


def test_g08_ptrade_zero_diff_ignored(gdriver) -> None:
    """目标=市价 100,000 → 差 0 忽略（无订单无事件）。"""
    _with_position(gdriver)
    snap = run_ptrade_golden(gdriver, PTRADE_G08_NOOP)
    assert snap.orders == []
    assert snap.order_events == []


PTRADE_G08_SELL = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        order_target_value('600000.SS', 96300)
        order_target_value('600000.SS', 134500)
"""


def test_g08_ptrade_sell_direction_and_boundary(gdriver) -> None:
    """96,300 → 卖 300（370 floor）; 134,500 → 买 3,400（3,450 floor）。"""
    _with_position(gdriver)
    snap = run_ptrade_golden(gdriver, PTRADE_G08_SELL)
    assert [o.side for o in snap.orders] == [OrderDirection.SELL, OrderDirection.BUY]
    assert [o.qty for o in snap.orders] == [300, 3_400]
    assert all(o.status is not None for o in snap.orders)
