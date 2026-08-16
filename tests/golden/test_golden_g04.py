# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:30:00
# @description : g04 涨停一字板买入（不成交）：expire + 一字板 info_json + degradation

"""黄金用例 g04：涨停一字板买入不成交（测试方案 §5 g04）。

场景：单标的平坦 10.0；day14 15:00 挂大额买单；day15 一字涨停
（open=high=low=close=涨停价），全天无成交 → 当日单 expire。

断言：
- 订单 EXPIRED；fills 空；现金/持仓不变（无成交、无冻结泄漏）；
- order_events 有 EXPIRE 事件且 info_json 含 one_word_limit 标记；
- run 降级清单非空、status=completed_degraded；
- 对照：正常日新单正常成交（g04 对照组）。
"""

from __future__ import annotations

from dataclasses import replace

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import load_expected, make_bars
from .daily import DailyDriver
from .framework import MockBroker, assert_six

CODE = "600000.SH"
N = 20
PX = 10.0
QTY = 50_000
SLIP = 0.001
FILL_PX = round(PX * (1.0 + SLIP), 4)  # 10.01


def _bars() -> list:
    """平坦数据, day15（index 14）标记一字涨停板（真实撮合语义, 5.3.2）。"""
    bars = make_bars(CODE, [PX] * N)
    bars[14] = replace(bars[14], limit_up=True)  # open==high==low==close 且触板 → is_one_word_limit
    return bars


def test_g04_one_word_board_expire() -> None:
    """一字涨停整日不成交→expire+标记；现金/持仓无变化。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = _bars()
    driver.add_data({CODE: bars})

    d14 = bars[13].date

    def _limit_buy_day14() -> None:
        o = driver.order(CODE, OrderDirection.BUY, QTY)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "one_word_board"})

    driver.on(d14, _limit_buy_day14)
    snap = driver.run()

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

    # 现金流水为空 → 无成交无冻结泄漏
    assert snap.cash.available == []

    # 手算 oracle（§2.4：expected/g04.json）：一字板日无成交 → NAV 恒 1.0、费用 0
    EXP = load_expected("g04")
    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    for cp in EXP["checkpoints_primary"]:
        d = bars[cp["day"] - 1].date  # JSON 的 day=1-based 交易日序号
        assert abs(nav_map[d] - cp["nav"]) <= 1e-10, f"day{cp['day']} nav != {cp['nav']}"
    assert snap.fee_total == EXP["fees"]["total"]

    # 一字板 info_json 标记 + degradation 记录
    expire_evts = [e for e in snap.order_events if e.event_type.value == "expire"]
    assert len(expire_evts) == 1
    assert expire_evts[0].info_json is not None
    assert expire_evts[0].info_json.get("one_word_limit") is True
    assert len(snap.degradations) == 1
    assert snap.degradations[0].startswith("g-golden-o1")


def test_g04_normal_day_sibling_fills() -> None:
    """对照组：非一字涨停日新单正常成交（同一批订单只在涨停日挂）。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = _bars()
    driver.add_data({CODE: bars})

    d14, d16 = bars[13].date, bars[15].date

    def _limit_buy_day14() -> None:
        o = driver.order(CODE, OrderDirection.BUY, QTY)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "one_word_board"})

    def _normal_buy_day16() -> None:
        o = driver.order(CODE, OrderDirection.BUY, QTY)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "fill", "price": FILL_PX})

    driver.on(d14, _limit_buy_day14)
    driver.on(d16, _normal_buy_day16)
    snap = driver.run()

    statuses = {o.order_id: o.status for o in snap.orders}
    assert statuses["g-golden-o1"] is OrderStatus.EXPIRED
    assert statuses["g-golden-o2"] is OrderStatus.FILLED
    assert len(snap.fills) == 1
    assert abs(snap.fills[0].price - FILL_PX) <= 1e-10

    # 手算 oracle（§2.4）：对照组 day17 成交后 NAV=0.99944995
    EXP = load_expected("g04")
    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    for cp in EXP["checkpoints_sibling"]:
        d = bars[cp["day"] - 1].date
        assert abs(nav_map[d] - cp["nav"]) <= 1e-10, f"对照组 day{cp['day']} nav != {cp['nav']}"
