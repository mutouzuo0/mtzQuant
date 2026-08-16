# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : T-U07 Account 账户测试（设计 5.5/4.4/3.14）：成本轨迹+现金四分类恒等式

"""T-U07：Account 账户（设计 5.5 / 4.4 / 3.14）：买入加权成本轨迹（分批建仓）、卖出
avg_cost 不变、清仓移除、现金四分类恒等式（total=available+receivable+frozen）。
"""

from __future__ import annotations

from datetime import datetime as dt

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.account import Account
from mtzquant.engine.orders import Fill, OrderDirection

T = dt(2026, 8, 15, 9, 35)


def _account(cash: float = 100_000.0) -> Account:
    return Account(run_id="run-1", initial_cash=cash, available_cash=cash)


def _fill(code: str, side: OrderDirection, qty: float, price: float, **kw) -> Fill:
    kw.setdefault("order_id", "ord-1")
    defaults = dict(order_id="ord-1", code=code, side=side, price=price, volume=qty, fill_time=T)
    defaults.update(kw)
    f = Fill(**defaults)
    return f


def test_buy_sets_weighted_avg_cost() -> None:
    """分批建仓加权成本轨迹：100×10 再 100×12 → avg=11（raw 价加权，不含费用）。"""
    acct = _account()
    acct.apply_fill(_fill("600000.SH", OrderDirection.BUY, 100.0, 10.0))
    pos = acct.positions["600000.SH"]
    assert pos.total_qty == 100.0
    assert pos.avg_cost == 10.0
    assert pos.today_qty == 100.0  # 当日买入计入 today（T+1 冻结）

    acct.apply_fill(_fill("600000.SH", OrderDirection.BUY, 100.0, 12.0))
    assert pos.total_qty == 200.0
    assert pos.avg_cost == pytest.approx(11.0)


def test_sell_keeps_avg_cost_and_clears_position() -> None:
    """卖出 avg_cost 不变；清仓后持仓移除。"""
    acct = _account()
    acct.apply_fill(_fill("600000.SH", OrderDirection.BUY, 200.0, 10.0))
    pos = acct.positions["600000.SH"]
    # 昨日买入 200（模拟 settle_day 后 today 清零）
    acct.settle_day()
    assert pos.today_qty == 0.0

    acct.apply_fill(_fill("600000.SH", OrderDirection.SELL, 150.0, 12.0))
    assert pos.total_qty == 50.0
    assert pos.avg_cost == 10.0  # 成本不变
    # 现金 = 100000 - 买入2000 + 卖出1800（fill 费用在引擎层挂载，此处 0）
    assert acct.available_cash == pytest.approx(99_800.0)

    acct.apply_fill(_fill("600000.SH", OrderDirection.SELL, 50.0, 13.0))
    assert "600000.SH" not in acct.positions  # 清仓移除


def test_t_plus_one_sell_guard() -> None:
    """T+1：当日买入不可卖（closeable 只含非今日量）。"""
    acct = _account()
    acct.apply_fill(_fill("600000.SH", OrderDirection.BUY, 100.0, 10.0))
    pos = acct.positions["600000.SH"]
    assert pos.closeable_qty == 0.0
    with pytest.raises(MtzQuantError, match="超过可卖"):
        acct.apply_fill(_fill("600000.SH", OrderDirection.SELL, 50.0, 11.0))

    acct.settle_day()  # 次日
    assert pos.closeable_qty == 100.0
    acct.apply_fill(_fill("600000.SH", OrderDirection.SELL, 100.0, 11.0))  # 可卖


def test_cash_invariant_buy_sell_roundtrip() -> None:
    """恒等式在买入卖出全程保持；等量等假买卖后现金回笼（fill 费用 0 分支）。"""
    acct = _account(cash=10_000.0)
    acct.apply_fill(_fill("510300.SH", OrderDirection.BUY, 500.0, 10.0))
    acct.assert_invariant()
    assert acct.available_cash == pytest.approx(5_000.0)  # 500×10
    acct.settle_day()
    acct.apply_fill(_fill("510300.SH", OrderDirection.SELL, 500.0, 10.0))
    acct.assert_invariant()
    assert acct.available_cash == pytest.approx(10_000.0)  # 现金完整回笼
    assert "510300.SH" not in acct.positions


def test_cash_four_class_identity() -> None:
    """现金四分类：total = available + receivable + frozen 各个环节成立。"""
    acct = _account(cash=50_000.0)
    assert acct.total_cash == 50_000.0
    acct.freeze_cash(8_000.0)
    assert acct.frozen_cash == 8_000.0
    assert acct.available_cash == 42_000.0
    assert acct.receivable_cash == 0.0
    acct.assert_invariant()

    acct.credit_dividend(500.0)  # ex_date 分红应收
    assert acct.receivable_cash == 500.0
    acct.assert_invariant()

    acct.settle_dividend()  # pay_date 到账
    assert acct.receivable_cash == 0.0
    assert acct.available_cash == pytest.approx(42_500.0)
    acct.assert_invariant()

    acct.release_frozen_cash(8_000.0)
    assert acct.available_cash == pytest.approx(50_500.0)
    acct.assert_invariant()


def test_freeze_bounds_and_day_settle() -> None:
    """冻结/释放超界报错；settle_day 释放全部冻结并清零今日买入。"""
    acct = _account(cash=1_000.0)
    with pytest.raises(MtzQuantError, match="冻结金额非法"):
        acct.freeze_cash(2_000.0)
    acct.freeze_cash(300.0)
    with pytest.raises(MtzQuantError, match="释放冻结金额非法"):
        acct.release_frozen_cash(999.0)
    acct.apply_fill(_fill("600000.SH", OrderDirection.BUY, 100.0, 3.0))  # 300
    assert acct.positions["600000.SH"].today_qty == 100.0
    acct.settle_day()
    assert acct.frozen_cash == 0.0
    assert acct.positions["600000.SH"].today_qty == 0.0


def test_position_market_value_and_validation() -> None:
    """市值=估值价×数量；买入/卖出参数非法报错；清仓复位成本。"""
    acct = _account()
    acct.apply_fill(_fill("600000.SH", OrderDirection.BUY, 200.0, 10.0))
    pos = acct.positions["600000.SH"]
    pos.last_price = 12.5
    assert pos.market_value == pytest.approx(2_500.0)
    assert pos.closeable_qty == 0.0

    with pytest.raises(MtzQuantError, match="买入参数非法"):
        pos.buy(0.0, 10.0)
    with pytest.raises(MtzQuantError, match="卖出参数非法"):
        pos.sell(-1.0, 10.0)
    with pytest.raises(MtzQuantError, match="超过可卖"):
        pos.sell(50.0, 10.0)  # today_qty=200 未 roll

    # 负初始资金
    with pytest.raises(MtzQuantError, match="初始资金不能为负"):
        Account(run_id="r", initial_cash=-1.0, available_cash=-1.0)


def test_market_value_uses_raw_price() -> None:
    """估值基准=最新成交/市场 raw 价（3.14：不复权、不含滑点的档案价）。"""
    acct = _account()
    acct.apply_fill(_fill("510300.SH", OrderDirection.BUY, 1000.0, 3.0))
    pos = acct.positions["510300.SH"]
    assert pos.market_value == pytest.approx(3_000.0)
    assert pos.last_price == 3.0
