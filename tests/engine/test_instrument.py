# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : T-U05 品种档案 InstrumentProfile 测试（设计 5.4）

"""T-U05：InstrumentProfile 品种档案（设计 5.4）：整手取整 / T+1 / 涨跌停 / 费率档案化。"""

from __future__ import annotations

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import InstrumentType
from mtzquant.engine.instrument import (
    Board,
    FeeParams,
    InstrumentProfile,
    LimitRule,
    MarginMode,
    convertible_bond_profile,
    etf_profile,
    stock_profile,
)


def test_lot_round_floor_semantics() -> None:
    """整手取整 floor(q/lot)×lot：股票/ETF 100、可转债 10，一律向下。"""
    stock = stock_profile("600000.SH")
    assert stock.lot_round(350.0) == 300.0
    assert stock.lot_round(100.0) == 100.0
    assert stock.lot_round(99.0) == 0.0  # 不足一手 → 空单（差量归零，设计 §4.5）
    assert etf_profile("510300.SH").lot_round(101.0) == 100.0
    assert convertible_bond_profile("113050.SH").lot_round(25.0) == 20.0


def test_builtin_profile_parameters() -> None:
    """内置档案参数：品种/整手/T+1/板块涨跌停/费率全部符合 A 股规则。"""
    stock = stock_profile("600000.SH")
    assert stock.instrument_type is InstrumentType.STOCK
    assert stock.lot_size == 100 and stock.t_plus == 1
    assert stock.margin_mode is MarginMode.FULL_CASH
    assert stock.contract_multiplier == 1.0
    assert stock.price_tick == 0.01
    assert stock.limit_rule.board is Board.MAIN

    etf = etf_profile("510300.SH")
    assert etf.instrument_type is InstrumentType.ETF
    assert etf.lot_size == 100 and etf.t_plus == 1
    # ETF 免印花税、免过户费在档案参数中体现（设计 5.4 / 8.3.3）
    assert etf.fee.stamp_tax_rate == 0.0
    assert etf.fee.transfer_fee_rate == 0.0
    assert etf.fee.commission_rate == 0.0001

    assert stock.fee.stamp_tax_rate == 0.001
    assert stock.fee.transfer_fee_rate == 0.00001

    cb = convertible_bond_profile("113050.SH")
    assert cb.instrument_type is InstrumentType.CONVERTIBLE_BOND
    assert cb.lot_size == 10 and cb.t_plus == 0  # 可转债 T+0


def test_limit_map_main_board() -> None:
    """主板 ±10%。"""
    down, up = LimitRule(board=Board.MAIN).limit_map(10.0)
    assert (down, up) == (9.0, 11.0)


def test_limit_map_rounds_to_cent() -> None:
    """涨跌停价 round 到 0.01（9.97 的 ±10% 区间 8.97–10.97）。"""
    down, up = LimitRule(board=Board.MAIN).limit_map(9.97)
    assert down == pytest.approx(8.97)
    assert up == pytest.approx(10.97)


def test_limit_map_st_ratio() -> None:
    """ST 抑制到 ±5%（仅主板）。"""
    down, up = LimitRule(board=Board.MAIN).limit_map(10.0, is_st=True)
    assert (down, up) == (9.5, 10.5)
    assert LimitRule(board=Board.MAIN).ratio(is_st=True) == 0.05
    assert LimitRule(board=Board.MAIN).ratio(is_st=False) == 0.10


def test_limit_map_growth_boards() -> None:
    """创业/科创板 ±20%、北交所 ±30%。"""
    for board, expect in [(Board.GEM, 8.0), (Board.STAR, 8.0), (Board.BSE, 7.0)]:
        down, up = LimitRule(board=board).limit_map(10.0)
        assert down == pytest.approx(expect)
        assert up == pytest.approx(20.0 - expect)
    # 创业/科创板无 ST 语义：is_st 不影响幅度
    assert LimitRule(board=Board.GEM).ratio(is_st=True) == 0.20


def test_limit_map_rejects_nonpositive_prev_close() -> None:
    rule = LimitRule(board=Board.MAIN)
    for bad in (0.0, -1.5):
        with pytest.raises(MtzQuantError, match="昨收价必须为正"):
            rule.limit_map(bad)


def test_profile_delegates_limit_map() -> None:
    down, up = stock_profile("600000.SH").limit_map(10.0)
    assert (down, up) == (9.0, 11.0)


def test_t_plus_one_sellable_quantity() -> None:
    """股票/ETF T+1：当日买入部分冻结，不可卖。"""
    lots = {"2026/08/10": 300.0, "2026/08/11": 200.0, "today": 500.0}
    assert stock_profile("600000.SH").sellable_quantity(lots) == 500.0
    assert etf_profile("510300.SH").sellable_quantity(lots) == 500.0


def test_t_plus_zero_sellable_quantity() -> None:
    """可转债 T+0：全部可卖（含当日买入）。"""
    lots = {"today": 200.0, "2026/08/11": 100.0}
    assert convertible_bond_profile("113050.SH").sellable_quantity(lots) == 300.0


def test_profile_validation() -> None:
    rule = LimitRule(Board.MAIN)
    base = dict(code="x", instrument_type=InstrumentType.STOCK, limit_rule=rule)
    with pytest.raises(MtzQuantError, match="lot_size 必须为正整数"):
        InstrumentProfile(**base, lot_size=0)
    with pytest.raises(MtzQuantError, match="lot_size 必须为正整数"):
        InstrumentProfile(**base, lot_size=3.5)
    with pytest.raises(MtzQuantError, match="t_plus 不能为负"):
        InstrumentProfile(**base, t_plus=-1)


def test_defaults_and_frozen() -> None:
    rule = LimitRule(Board.MAIN)
    p = InstrumentProfile(code="x.SH", instrument_type=InstrumentType.ETF, limit_rule=rule)
    assert isinstance(p.fee, FeeParams)
    assert p.fee.commission_min == 5.0
    with pytest.raises(AttributeError):
        p.lot_size = 200  # frozen
