# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : T-U06 撮合模型测试（设计 5.3.3）：FeeModel 费用边界 + 五模型契约

"""T-U06：撮合模型（设计 5.3.3）：FeeModel 费用边界（最低佣金/ETF 免印花税/沪过户费/四
项分账）+ Fill/Slippage/Liquidity/Latency 默认实现与参数守卫。
"""

from __future__ import annotations

from datetime import datetime as dt

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.instrument import etf_profile, stock_profile
from mtzquant.engine.models.bar import MinimalBar
from mtzquant.engine.models.fee import FeeBreakdown, FeeModel
from mtzquant.engine.models.fill_price import FillModel, PriceBasis
from mtzquant.engine.models.latency import LatencyModel
from mtzquant.engine.models.liquidity import LiquidityBasis, LiquidityModel
from mtzquant.engine.models.slippage import SlippageModel
from mtzquant.engine.orders import OrderDirection

STOCK = stock_profile("600000.SH")
ETF = etf_profile("510300.SH")
T = dt(2026, 8, 15, 9, 30)


def _bar(**overrides) -> MinimalBar:
    base: dict = dict(
        dt=T,
        open=10.0,
        high=10.5,
        low=9.8,
        close=10.2,
        volume=1_000_000.0,
        pre_close=10.0,
    )
    base.update(overrides)
    return MinimalBar(**base)


# ---------------------------------------------------------------- FeeModel


def test_commission_floor_min_5() -> None:
    """佣金 = max(5, 0.0001×amount)：小额触发最低佣金。"""
    fee = FeeModel().compute(qty=100.0, price=10.0, profile=STOCK, side=OrderDirection.BUY)
    assert fee.commission == 5.0  # 0.0001×1000=0.1 → max=5


def test_commission_by_rate_when_above_floor() -> None:
    """大额按率计：0.0001×100000=10 > 5。"""
    fee = FeeModel().compute(qty=10_000.0, price=10.0, profile=STOCK, side=OrderDirection.SELL)
    assert fee.commission == pytest.approx(10.0)


def test_stamp_tax_sell_only_stock_only() -> None:
    """印花税仅卖出且仅股票：股票卖出收 0.001×额、买入不收。"""
    sell = FeeModel().compute(qty=1_000.0, price=10.0, profile=STOCK, side=OrderDirection.SELL)
    buy = FeeModel().compute(qty=1_000.0, price=10.0, profile=STOCK, side=OrderDirection.BUY)
    assert sell.stamp_tax == pytest.approx(10.0)
    assert buy.stamp_tax == 0.0


def test_etf_no_stamp_tax_both_sides() -> None:
    """ETF 买卖均无印花税、无过户费（档案 stamp_tax_rate/transfer_fee_rate=0）。"""
    for side in (OrderDirection.BUY, OrderDirection.SELL):
        fee = FeeModel().compute(qty=1_000.0, price=10.0, profile=ETF, side=side)
        assert fee.stamp_tax == 0.0
        assert fee.transfer_fee == 0.0


def test_transfer_fee_shanghai_stock_only() -> None:
    """过户费仅沪市股票（沪 profile 有值）：0.00001×10000=0.1。"""
    fee = FeeModel().compute(qty=1_000.0, price=10.0, profile=STOCK, side=OrderDirection.BUY)
    assert fee.transfer_fee == pytest.approx(0.1)


def test_fee_breakdown_sums_correctly() -> None:
    """四项分账合计与逐项一致（design 8.3.3 对账：commission+stamp+transfer=total）。"""
    fee = FeeModel().compute(qty=5_000.0, price=12.34, profile=STOCK, side=OrderDirection.SELL)
    assert fee.total == pytest.approx(fee.commission + fee.stamp_tax + fee.transfer_fee)
    assert isinstance(fee, FeeBreakdown)
    assert round(fee.commission, 4) == fee.commission  # 分账精度 4 位小数


def test_fee_rejects_invalid_inputs() -> None:
    model = FeeModel()
    with pytest.raises(MtzQuantError, match="费用计算输入非法"):
        model.compute(qty=-1.0, price=10.0, profile=STOCK, side=OrderDirection.BUY)
    with pytest.raises(MtzQuantError, match="费用计算输入非法"):
        model.compute(qty=100.0, price=0.0, profile=STOCK, side=OrderDirection.BUY)


# ---------------------------------------------------------------- FillModel


def test_fill_price_next_open_ask_bid_proxy() -> None:
    """默认 next_open：买入 ask 侧代理 open×(1+hs)、卖出 bid 侧代理 open×(1-hs)。"""
    bar = _bar(open=10.0)
    model = FillModel()  # half_spread=0.001
    assert model.fill_price(bar, OrderDirection.BUY) == pytest.approx(10.01)
    assert model.fill_price(bar, OrderDirection.SELL) == pytest.approx(9.99)


def test_fill_price_basis_same_close() -> None:
    model = FillModel(basis=PriceBasis.SAME_CLOSE)
    assert model.fill_price(_bar(close=10.2), OrderDirection.BUY) == pytest.approx(10.2 * 1.001)


# ---------------------------------------------------------------- SlippageModel


def test_slippage_buy_up_sell_down() -> None:
    model = SlippageModel(ratio=0.001, fixed=0.01)
    assert model.apply(10.0, OrderDirection.BUY) == pytest.approx(10.02)
    assert model.apply(10.0, OrderDirection.SELL) == pytest.approx(9.98)


def test_slippage_validation() -> None:
    with pytest.raises(MtzQuantError, match="滑点参数不能为负"):
        SlippageModel(ratio=-0.1)
    with pytest.raises(MtzQuantError, match="成交基准价必须为正"):
        SlippageModel().apply(0.0, OrderDirection.BUY)


# ---------------------------------------------------------------- LiquidityModel


def test_liquidity_participation_cap() -> None:
    """单笔 ≤ 基准量×10%：200 万成量 → 上限 20 万（超量截断到上限）。"""
    model = LiquidityModel()
    cap_over = model.max_qty(order_qty=500_000.0, reference_volume=2_000_000.0)
    cap_fit = model.max_qty(order_qty=50_000.0, reference_volume=2_000_000.0)
    assert cap_over == pytest.approx(200_000.0)
    assert cap_fit == pytest.approx(50_000.0)
    assert model.basis is LiquidityBasis.PREV_ADV


def test_liquidity_stress_and_derived() -> None:
    """0.25 为压力上限；with_participation 派生副本不改原型（报告多档敏感性）。"""
    base = LiquidityModel()
    stress = LiquidityModel(max_participation=0.25)
    assert stress.max_qty(1_000_000.0, 2_000_000.0) == pytest.approx(500_000.0)
    s1 = base.with_participation(0.05)
    assert s1.max_participation == 0.05 and base.max_participation == 0.10
    assert s1.basis is LiquidityBasis.PREV_ADV


def test_liquidity_validation() -> None:
    with pytest.raises(MtzQuantError, match="max_participation 必须在"):
        LiquidityModel(max_participation=0.0)
    with pytest.raises(MtzQuantError, match="max_participation 必须在"):
        LiquidityModel(max_participation=1.5)
    with pytest.raises(MtzQuantError, match="不能为负"):
        LiquidityModel().max_qty(order_qty=-1.0, reference_volume=100.0)


# ---------------------------------------------------------------- LatencyModel


def test_latency_default_next_bar() -> None:
    """默认 bars_delay=1：下单后下一撮合事件才可成交（日线=次日开盘）。"""
    model = LatencyModel()
    assert model.bars_delay == 1
    assert model.effective_sequence(submitted_sequence=10) == 11


def test_latency_validation() -> None:
    with pytest.raises(MtzQuantError, match="bars_delay 不能为负"):
        LatencyModel(bars_delay=-1)


# ---------------------------------------------------------------- MinimalBar


def test_minimal_bar_one_word_limit() -> None:
    """一字板：open==high==low==close 且触及停价；停牌不算。"""
    assert _bar(open=11.0, high=11.0, low=11.0, close=11.0, limit_up=True).is_one_word_limit
    assert not _bar(open=10.0, high=10.5, low=9.8, close=10.2).is_one_word_limit
    one_word = _bar(open=11.0, high=11.0, low=11.0, close=11.0, limit_up=True)
    assert not MinimalBar(
        dt=one_word.dt,
        open=one_word.open,
        high=one_word.high,
        low=one_word.low,
        close=one_word.close,
        volume=one_word.volume,
        suspended=True,
        limit_up=True,
    ).is_one_word_limit
