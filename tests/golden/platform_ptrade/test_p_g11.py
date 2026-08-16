# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 12:40:00
# @update_time        : 2026/08/16 21:59:08
# @description : g11/g12/g13 平台版（PTrade）：分红送转 NAV 连续 + 退市冻结 + 收盘单次日开盘

"""g11/g12/g13 平台版（PTrade, D3）。手算依据与 native 一致:
- g11: 持 20,000@10, ex(day20) 应收 10,000+送转×2(40,000@4.75), pay(day25) 到账; NAV 三点全 1.2
- g12: C 退市后估值冻结 8.0 + stale 标记 + open_positions 2→1; NAV 恒 1.18
- g13: day10 15:00 挂单 → day11 09:30 成交（时点戳断言）
"""

from __future__ import annotations

from datetime import datetime

from mtzquant.engine.orders import OrderDirection, OrderStatus
from tests.golden.conftest import flat_series, load_expected, make_bars  # noqa: F401
from tests.golden.daily import DailyDriver
from tests.golden.framework import MockBroker, assert_six

from .bridge import run_ptrade_golden

# ============ g11 分红送转 ============
CODE = "600156.SH"
N = 30
QTY = 20_000
PX = 10.0
DIV_PER = 0.5
BONUS = 2.0
EX_PX = 4.75

EXP = load_expected("g11")


def _g11_driver() -> DailyDriver:
    closes = [PX] * 19 + [EX_PX] * (N - 19)
    driver = DailyDriver(MockBroker(), initial_cash=1_000_000.0, initial_positions={CODE: QTY})
    driver.add_data({CODE: make_bars(CODE, closes, start="2026-01-05")})
    return driver


def _corp_ex(driver: DailyDriver) -> None:
    """day20 开盘前: 送转 ×2 + 应收股息（native 同构, 3.14 三时点）。"""
    pos = driver.account.positions[CODE]
    pos.apply_share_change(BONUS)
    driver.account.credit_dividend(EXP["dividend_amount"])


PTRADE_G11 = """\
def initialize(context):
    g.n = 0
    set_universe(['600156.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 20:
        p = get_position('600156.SS')
        g.qty_at_ex = p.amount
        g.cost_at_ex = p.cost_basis
"""


def test_g11_ptrade_cash_div_and_bonus() -> None:
    driver = _g11_driver()
    bars = driver.data[CODE]
    d19, d20, d25 = bars_dates(bars, 18, 19, 24)
    driver.on_day_open(d20, lambda: _corp_ex(driver))
    driver.on_dividend_pay(d25)

    import json as _json
    import pathlib
    import tempfile

    probe = pathlib.Path(tempfile.mkdtemp()) / "probe.json"
    script = (
        f"PROBE = r'{probe}'\n" + PTRADE_G11 + "\ndef after_trading_end(context):\n"
        "    import json as _json\n"
        "    with open(PROBE, 'w') as _f:\n"
        "        _json.dump({'qty': g.get('qty_at_ex'), 'cost': g.get('cost_at_ex')}, _f)\n"
    )
    snap = run_ptrade_golden(driver, script)
    result = _json.loads(probe.read_text(encoding="utf-8"))

    # day20 策略回调已见新数量（阶段②先于⑥, ptrade 投影一致）
    assert result["qty"] == EXP["shares_after"] == 40_000
    assert abs(result["cost"] - EXP["avg_cost_after"]) <= 1e-9  # 5.0

    end = EXP["end_state"]
    pos_end = end["position"]
    assert_six(
        snap,
        positions={
            CODE: {
                "total_qty": pos_end["total_qty"],
                "avg_cost": pos_end["avg_cost"],
                "last_price": pos_end["last_price"],
                "market_value": pos_end["market_value"],
            },
        },
        status=end["status"],
    )
    assert abs(driver.account.available_cash - end["available_cash"]) <= 1e-6
    assert driver.account.receivable_cash == end["receivable_cash"]

    # NAV 连续性（oracle day19/20/25 全 = 1.2）
    chk = {cp["day"]: cp["nav"] for cp in EXP["checkpoints"]}
    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    assert abs(nav_map[d19] - chk[19]) <= 1e-10
    assert abs(nav_map[d20] - chk[20]) <= 1e-10
    assert abs(nav_map[d25] - chk[25]) <= 1e-10


def bars_dates(bars_map: dict, *idx: int) -> tuple[str, ...]:
    """driver.data 的字符串日期按键序取第 idx 位（辅助）。"""
    dates = sorted(bars_map.keys())
    return tuple(dates[i] for i in idx)


# ============ g12 退市冻结 ============
CODE_A = "600001.SH"
CODE_C = "600002.SH"
PX_A, PX_C = 10.0, 8.0
QTY12 = 10_000


def _g12_driver() -> DailyDriver:
    driver = DailyDriver(
        MockBroker(),
        initial_cash=1_000_000.0,
        initial_positions={CODE_A: QTY12, CODE_C: QTY12},
    )
    bars_a = make_bars(CODE_A, [PX_A] * 50, start="2026-01-05")
    bars_c = make_bars(CODE_C, [PX_C] * 40, start="2026-01-05")
    driver.add_data({CODE_A: bars_a, CODE_C: bars_c})
    driver.on_delist(CODE_C, bars_c[39].date)
    return driver


PTRADE_G12 = """\
def initialize(context):
    g.n = 0
    set_universe(['600001.SS', '600002.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 45:
        o = order('600002.SS', -1000)
        g.rej = (o.status, )
"""


def test_g12_ptrade_delist_frozen_and_reject() -> None:
    driver = _g12_driver()
    snap = run_ptrade_golden(driver, PTRADE_G12)

    nav_map = {
        p.dt.date().isoformat(): (p.nav, p.stale_codes, p.open_positions) for p in snap.nav_series
    }
    dates = [s.bar.date for s in driver.sessions]
    d39, d44 = dates[39], dates[44]
    assert abs(nav_map[d39][0] - 1.18) <= 1e-10  # 最后交易日
    assert abs(nav_map[d44][0] - 1.18) <= 1e-10  # 冻结期连续
    assert CODE_C in nav_map[d44][1]  # stale 标记
    assert nav_map[d44][2] == 1  # open_positions 2→1
    # 退市后新单 rejected（ptrade 回执状态=9 废单）
    statuses = [o.status for o in snap.orders]
    assert OrderStatus.REJECTED in statuses


# ============ g13 收盘单次日开盘时序 ============
CODE13 = "600000.SH"


PTRADE_G13 = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.SS'])


def handle_data(context, data):
    g.n += 1
    if g.n == 10:
        order('600000.SS', 10000)
"""


def test_g13_ptrade_close_order_next_open(gdriver) -> None:
    """day10 15:00 挂单 → day11 09:30 成交（时点戳, 4.7 时序）。"""
    gdriver.add_data({CODE13: flat_series(CODE13, 20)})
    snap = run_ptrade_golden(gdriver, PTRADE_G13)
    bars = flat_series(CODE13, 20)
    d11 = bars[10].date

    assert len(snap.fills) == 1
    fill = snap.fills[0]
    assert fill.fill_time == datetime.strptime(f"{d11} 09:30", "%Y-%m-%d %H:%M")
    assert abs(fill.price - 10.01) <= 1e-10  # NEXT_OPEN × (1+0.001)
    assert fill.volume == 10_000
    assert fill.side is OrderDirection.BUY
