# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/16 00:05:00
# @description : g10 盘前/盘中/盘后调度时点可见性：bar 可见性与账户状态快照

"""黄金用例 g10：盘前/盘中/盘后调度时点可见性（测试方案 §5 g10）。

场景：单标的 5 日（收盘 10.0/10.2/10.4/10.6/10.8 递增）；day3 有成交：
- day2 15:00 挂买单（order_target_value 50,000）→ day3 开盘 10.2×(1+0.001) 成交；
- day3 盘前（before_open）：当日 bar 不可见（cutoff=昨日），账户未含当日成交；
- day3 盘中（on_daily_close，15:00 回调）：当日 bar 可见、账户已含当日开盘成交；
- day3 盘后（after_close）：同盘中数据环境 + 成交回报齐全。

三条回调各自把可见快照写入 g.record，run 后逐字段断言。
"""

from __future__ import annotations

from datetime import datetime

from mtzquant.engine.orders import OrderStatus

from .conftest import make_bars
from .daily import DailyDriver
from .framework import MockBroker

CODE = "600000.SH"
N = 5
CLOSES = [10.0, 10.2, 10.4, 10.6, 10.8]
SLIP = 0.001


def test_g10_schedule_point_visibility() -> None:
    """before_open 只见昨收；on_daily_close/after_close 见当日+成交。"""
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    bars = make_bars(CODE, CLOSES)
    driver.add_data({CODE: bars})

    d2, d3 = bars[2].date, bars[3].date
    FILL_PX = round(bars[3].open * (1.0 + SLIP), 4)  # 10.2×1.001=10.2102
    record: dict[str, dict] = {}

    def _buy_day2() -> None:
        o = driver.order_target_value(CODE, 100_000.0)
        assert o.status is OrderStatus.PENDING
        broker.program(o.order_id, {"type": "fill", "price": FILL_PX})

    def _before_open_d3() -> None:
        # 盘前：当日 bar 不可见 → history 止于昨日 close=10.4；账户无持仓
        hist = driver.history(CODE, 10)
        record["before_open"] = {
            "last_close": hist[-1].close,
            "len_hist": len(hist),
            "pos_qty": driver.account.positions.get(CODE).total_qty
            if CODE in driver.account.positions
            else 0,
            "cash": round(driver.account.available_cash, 4),
        }

    def _on_close_d3() -> None:
        # 盘中（15:00）：当日 bar 可见、账户已含当日开盘成交
        hist = driver.history(CODE, 10)
        record["on_close"] = {
            "last_close": hist[-1].close,  # 10.6
            "len_hist": len(hist),
            "pos_qty": driver.account.positions.get(CODE).total_qty,
            "cash": round(driver.account.available_cash, 4),
        }

    def _after_close_d3() -> None:
        hist = driver.history(CODE, 10)
        record["after_close"] = {
            "last_close": hist[-1].close,
            "len_hist": len(hist),
            "fills_visible": len(driver._fills),  # noqa: SLF001
            "cash": round(driver.account.available_cash, 4),
        }

    driver.on(d2, _buy_day2)
    driver.on_before_open(d3, _before_open_d3)
    driver.on(d3, _on_close_d3)
    driver.on_after_close(d3, _after_close_d3)
    snap = driver.run()

    # ---- 盘前快照：只见昨收 10.4、无当日成交 ----
    pre = record["before_open"]
    assert pre["len_hist"] == 3  # day0..day2
    assert pre["last_close"] == 10.4
    assert pre["pos_qty"] == 0  # 当日成交尚未入账
    assert pre["cash"] == 1_000_000.0

    # ---- 盘中快照：当日 bar 可见（当前日=day3 → history 止于 10.6）+ 成交已入账 ----
    mid = record["on_close"]
    assert mid["len_hist"] == 4  # day0..day3
    assert mid["last_close"] == 10.6
    # 目标 100,000 / 昨收 10.4 → 9,615 → 整百 9,600
    assert mid["pos_qty"] == 9_600
    assert mid["cash"] < 1_000_000.0  # 已扣成交+费用

    # ---- 盘后快照：数据环境同盘中 + 成交回报齐全 ----
    post = record["after_close"]
    assert post["len_hist"] == 4
    assert post["last_close"] == 10.6
    assert post["fills_visible"] == 1
    assert post["cash"] == mid["cash"]

    # 成交对象形态（fill 计入回调可见性）
    assert snap.fills[0].fill_time == datetime.strptime(f"{d3} 09:30", "%Y-%m-%d %H:%M")
