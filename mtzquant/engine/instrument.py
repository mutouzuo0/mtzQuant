# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : InstrumentProfile 品种档案（设计 5.4）：整手/t_plus/涨跌停/费率全档案化

"""InstrumentProfile 品种档案（设计 5.4，多品种扩展的核心）。

撮合/记账/涨跌停判断不硬编码任何品种规则——一律查档案：
  整手取整 floor(q/lot_size)×lot_size（股票100/ETF 100/可转债10）
  T+1 可卖数量、涨跌停价（主板±10%/ST±5%/创科±20%/北交所±30%）、费率参数。
v1 内置 stock / etf 档案并覆盖 A 股规则；convertible_bond/futures/option
结构与参数位就位、随品种接入逐步填充（可转债 T+0、期货保证金/乘数等）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import InstrumentType


class MarginMode(StrEnum):
    """账户保证金模式（设计 5.4：期货/期权为 margin）。v1 恒 full_cash。"""

    FULL_CASH = "full_cash"
    MARGIN = "margin"


class Board(StrEnum):
    """板块（决定涨跌停幅度，设计 5.4 LimitRule）。"""

    MAIN = "main"  # 沪深主板 ±10%（ST ±5%）
    GEM = "gem"  # 创业板 300xxx/301xxx ±20%
    STAR = "star"  # 科创板 688xxx ±20%
    BSE = "bse"  # 北交所 8xxxxx/4xxxxx ±30%


@dataclass(frozen=True)
class LimitRule:
    """涨跌停规则：板块决定普通幅度、ST 抑制到 5%（主板语义）全档案化。"""

    board: Board
    st_ratio: float = 0.05

    @property
    def base_ratio(self) -> float:
        ratios = {Board.MAIN: 0.10, Board.GEM: 0.20, Board.STAR: 0.20, Board.BSE: 0.30}
        return ratios[self.board]

    def ratio(self, *, is_st: bool = False) -> float:
        """ST 仅对主板压到 st_ratio（创/科/北交所无 ST 语义，回归板块幅度）。"""
        if is_st and self.board is Board.MAIN:
            return self.st_ratio
        return self.base_ratio

    def limit_map(self, prev_close: float, *, is_st: bool = False) -> tuple[float, float]:
        """涨跌停价（round 到 0.01，A 股最小价位惯例，设计 5.4）。"""
        if prev_close <= 0:
            raise MtzQuantError(
                f"昨收价必须为正，得到 {prev_close}",
                stage="instrument",
                hint="涨跌停价按昨收计算（设计 5.4）",
            )
        r = self.ratio(is_st=is_st)
        return round(prev_close * (1 - r), 2), round(prev_close * (1 + r), 2)


@dataclass(frozen=True)
class FeeParams:
    """费率参数（设计 8.3.3 四项费用口径；全部来自档案，撮合层不硬编码费率）。"""

    commission_rate: float = 0.0001  # 佣金比率（沪深默认万 1）
    commission_min: float = 5.0  # 佣金最低 5 元
    stamp_tax_rate: float = 0.001  # 印花税：仅卖出、仅股票（ETF 置 0）
    transfer_fee_rate: float = 0.00001  # 过户费：仅沪市股票（双向），ETF 置 0


@dataclass(frozen=True)
class InstrumentProfile:
    """证券档案（设计 5.4 字段全集；可由 DB 表 instrument 覆盖，热更新规则）。"""

    code: str  # 内部统一代码（normalize_code 后，如 510300.SH）
    instrument_type: InstrumentType
    limit_rule: LimitRule
    lot_size: int = 100  # 股票100 / ETF 100 / 可转债 10 / 期货 1×乘数
    t_plus: int = 1  # 股票1 / ETF 1 / 可转债0 / 期货0 / 期权0
    margin_mode: MarginMode = MarginMode.FULL_CASH
    contract_multiplier: float = 1.0  # 期货合约乘数；股票/ETF=1.0
    price_tick: float = 0.01  # 股票0.01 / 期权0.0001 / 期货按合约
    fee: FeeParams = FeeParams()
    name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.lot_size, int) or self.lot_size <= 0:
            raise MtzQuantError(
                f"lot_size 必须为正整数，得到 {self.lot_size}",
                stage="instrument",
            )
        if self.t_plus < 0:
            raise MtzQuantError(f"t_plus 不能为负，得到 {self.t_plus}", stage="instrument")

    def lot_round(self, qty: float) -> float:
        """整手取整：floor(数量 / lot_size) × lot_size（设计 §4.5 归一流程）。"""
        lots = math.floor(qty / self.lot_size)
        return float(lots * self.lot_size)

    def limit_map(self, prev_close: float, *, is_st: bool = False) -> tuple[float, float]:
        """委托给 limit_rule（组合而非继承）。"""
        return self.limit_rule.limit_map(prev_close, is_st=is_st)

    def sellable_quantity(self, lots_by_buy_date: dict[str, float]) -> float:
        """T+1 可卖数量：非当日买入数量合计（设计 5.4 t_plus 语义）。

        参数为「买入日期字符串 → 买入数量」映射；键 'today' 表示当日买入部分。
        t_plus=0 品种（可转债等）全部可卖；股票/ETF（t_plus=1）当日买入部分冻结。
        """
        if self.t_plus == 0:
            return float(sum(lots_by_buy_date.values()))
        eligible = [qty for date, qty in lots_by_buy_date.items() if date != "today"]
        return float(sum(eligible))


def stock_profile(code: str, *, board: Board = Board.MAIN, name: str = "") -> InstrumentProfile:
    """内置 A 股档案：100 股整手、T+1、板块涨跌停、印花税卖出 0.001、过户费双向。"""
    return InstrumentProfile(
        code=code,
        instrument_type=InstrumentType.STOCK,
        name=name,
        lot_size=100,
        t_plus=1,
        limit_rule=LimitRule(board=board),
        fee=FeeParams(
            commission_rate=0.0001,
            commission_min=5.0,
            stamp_tax_rate=0.001,
            transfer_fee_rate=0.00001,
        ),
    )


def etf_profile(code: str, *, name: str = "") -> InstrumentProfile:
    """内置 ETF 档案：100 份整手、T+1、涨跌停±10%、免印花税/过户费。"""
    return InstrumentProfile(
        code=code,
        instrument_type=InstrumentType.ETF,
        name=name,
        lot_size=100,
        t_plus=1,
        limit_rule=LimitRule(board=Board.MAIN),
        fee=FeeParams(
            commission_rate=0.0001,
            commission_min=5.0,
            stamp_tax_rate=0.0,
            transfer_fee_rate=0.0,
        ),
    )


def convertible_bond_profile(code: str, *, name: str = "") -> InstrumentProfile:
    """内置可转债档案（结构就位）：10 张整手、T+0、免印花税/过户费。"""
    return InstrumentProfile(
        code=code,
        instrument_type=InstrumentType.CONVERTIBLE_BOND,
        name=name,
        lot_size=10,
        t_plus=0,
        limit_rule=LimitRule(board=Board.MAIN),
        fee=FeeParams(
            commission_rate=0.0002,
            commission_min=1.0,
            stamp_tax_rate=0.0,
            transfer_fee_rate=0.0,
        ),
    )
