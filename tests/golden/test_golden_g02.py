# coding:utf-8
# @author            : 木头左
# @create_time       : 2026/08/16 02:20:00
# @update_time       : 2026/08/16 21:59:08
# @description : g02 单次买卖（费用/整手）：真实 BrokerSim 买卖侧对称滑点手算断言

"""黄金用例 g02：单次买卖（测试方案 §5 g02，防自证最严用例）。

场景：单标的 60 日、价格平坦 10.0；day10 15:00 挂买单（order_target_value 目标
=总资产 50%=500,000）→ 整手 50,000 股；day20 15:00 全卖（目标 0）。
撮合语义（真实 BrokerSim, 5.3.3）：买=ask 侧 开盘×(1+0.001)=10.01，卖=bid 侧
开盘×(1−0.001)=9.99——买卖侧对称滑点（阶段 C 的单一价 10.01 为 MockBroker 约定,
阶段 F 换真实撮合后按对称滑点修正手算）。

手算（全部期望值以字面量写死，FILL_TOL=1e-10）：
- day11 买入成交 10.01×50,000=500,500；佣金=max(5, 0.0001×500,500)=50.05；印花税=0
  现金=1,000,000−500,500−50.05=499,449.95
- day11/day12/day20 NAV=(499,449.95+10.0×50,000)/1,000,000=0.99944995
- day21 卖出成交 9.99×50,000=499,500；佣金=max(5,0.0001×499,500)=49.95；
  印花税=0.0005×499,500=249.75；账面=清仓
  现金=499,449.95+499,500−49.95−249.75=998,650.25 → NAV=0.99865025
- 费用：commission 100.00 / stamp_tax 249.75 / transfer_fee 0 / total 349.75
"""

from __future__ import annotations

from datetime import datetime

from mtzquant.engine.orders import OrderDirection, OrderStatus

from .conftest import flat_series, load_expected
from .daily import DailyDriver
from .framework import MockBroker, assert_six

CODE = "600000.SH"
N = 60
PX = 10.0
SLIP = 0.001
QTY = 50_000
FILL_PX_BUY = round(PX * (1.0 + SLIP), 4)  # 10.01（买=ask 侧, 5.3.3）
FILL_PX_SELL = round(PX * (1.0 - SLIP), 4)  # 9.99（卖=bid 侧）
BUY_AMOUNT = round(FILL_PX_BUY * QTY, 2)  # 500,500.0
SELL_AMOUNT = round(FILL_PX_SELL * QTY, 2)  # 499,500.0
COMM_BUY = round(max(5.0, 0.0001 * BUY_AMOUNT), 2)  # 50.05
COMM_SELL = round(max(5.0, 0.0001 * SELL_AMOUNT), 2)  # 49.95
STAMP_SELL = round(0.0005 * SELL_AMOUNT, 2)  # 249.75

CASH_AFTER_BUY = round(1_000_000 - BUY_AMOUNT - COMM_BUY, 2)  # 499,449.95
CASH_AFTER_SELL = round(CASH_AFTER_BUY + SELL_AMOUNT - COMM_SELL - STAMP_SELL, 2)  # 998,650.25
NAV_HOLD = round((CASH_AFTER_BUY + PX * QTY) / 1_000_000, 10)  # 0.99944995
NAV_FINAL = round(CASH_AFTER_SELL / 1_000_000, 10)  # 0.99865025


def _dates() -> tuple[str, str, str, str, str, str]:
    bars = flat_series(CODE, N)
    return bars[8].date, bars[9].date, bars[10].date, bars[11].date, bars[19].date, bars[20].date


def test_g02_single_buy_sell() -> None:
    """单次买卖全链路：六要素 + NAV 检查点 字面量断言。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    driver.add_data({CODE: flat_series(CODE, N)})

    d9, d10, d11, d12, d20, d21 = _dates()

    def _buy() -> None:
        o = driver.order_target_value(CODE, 500_000.0)
        assert o.status is OrderStatus.PENDING
        assert o.qty == QTY

    def _sell() -> None:
        o = driver.order_target_value(CODE, 0.0)
        assert o.status is OrderStatus.PENDING
        assert o.qty == QTY

    driver.on(d10, _buy)
    driver.on(d20, _sell)
    snap = driver.run()

    # ---- 订单（2 单：买入/卖出，均全量成交） ----
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
        positions={},  # 清仓
        fees={
            "commission": round(COMM_BUY + COMM_SELL, 2),
            "stamp_tax": STAMP_SELL,
            "transfer_fee": 0.0,
        },
        status="completed_exact",
    )

    # ---- NAV 检查点（day11/12/20/21，§2.4：expected/g02.json 为 oracle） ----
    EXP = load_expected("g02")
    chk = {cp["day"]: cp["nav"] for cp in EXP["checkpoints"]}
    navs = [(p.dt.date().isoformat(), p.nav) for p in snap.nav_series]
    nav_map = dict(navs)
    assert abs(nav_map[d9] - 1.0) <= 1e-10  # 未持仓，day9 仍是 1.0
    assert abs(nav_map[d11] - chk[11]) <= 1e-10
    assert abs(nav_map[d12] - chk[12]) <= 1e-10
    assert abs(nav_map[d20] - chk[20]) <= 1e-10
    assert abs(nav_map[d21] - chk[21]) <= 1e-10
    assert len(navs) == N  # 逐日净值=交易日数
    # 费用 oracle + 手算-推导与 JSON 一致（防手算笔误/防自证）
    assert snap.fee_total == EXP["fees"]["total"]  # 349.75
    assert NAV_HOLD == chk[11] and NAV_FINAL == chk[21]
    assert CASH_AFTER_BUY == EXP["buy"]["cash_after"]
    assert CASH_AFTER_SELL == EXP["sell"]["cash_after"]


def test_g02_fees_min_commission_boundary() -> None:
    """佣金下限边界：小额成交 100 股×5.005=500.5 → 佣金=5.0 而非 0.05（g07 同构）。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = flat_series(CODE, N, price=5.0)
    driver.add_data({CODE: bars})
    d0 = bars[0].date

    def _small_buy() -> None:
        o = driver.order(CODE, OrderDirection.BUY, 100)  # 100 股, ask 侧 5.0×1.001=5.005
        assert o.status is OrderStatus.PENDING

    driver.on(d0, _small_buy)
    snap = driver.run()
    assert snap.fees["commission"] == 5.0  # >= min_commission=5
    assert snap.fees["stamp_tax"] == 0.0
    assert len(snap.fills) == 1
    assert abs(snap.fills[0].price - 5.005) <= 1e-10
    assert abs(snap.fills[0].amount - 500.5) <= 1e-10
