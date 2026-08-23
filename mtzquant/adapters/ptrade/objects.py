# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 10:40:00
# @update_time        : 2026/08/23 10:30:00
# @description : L1-L3 PTrade 官方对象投影：Context/Portfolio/Position/Order/BarData（4.7）

"""PTrade 官方对象投影（设计 4.7 / 附录C）。

- `PTradePosition/PTradePortfolio/PTradeContext`: 基于 shared/portfolio_view 的薄包装
  （只读 property 映射, 字段名与官方一致）。
- `PTradeOrder`: 官方订单回执（id/dt/symbol/amount 买正卖负/filled 带符号/entrust_no/
  cancel_entrust_no/priceGear/status）; status 由内部状态机动态映射。
- `PTRADE_STATUS`: 委托状态字典（0未报/2已报/5部撤/6已撤/7部成/8已成/9废单）;
  回测产出子集 {2,7,8,6,9}（0 未报是实盘报盘瞬间态, 回测不可达——已知近似, T-PT02 说明）。
- `PTradeBarData`: 日线全字段（分钟下 preclose/high_limit/low_limit/unlimited 补 0.0
  的行为留 M5, 字段位先占）。
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from mtzquant.adapters.shared.code_style import denormalize_code
from mtzquant.adapters.shared.portfolio_view import (
    UniformPortfolio,
    UniformPosition,
    ptrade_portfolio_view,
    ptrade_position_view,
)
from mtzquant.engine.orders import Order, OrderStatus

# PTrade 委托状态字典（4.7 / 附录C）
PTRADE_STATUS: dict[str, str] = {
    "0": "未报",
    "2": "已报",
    "5": "部撤",
    "6": "已撤",
    "7": "部成",
    "8": "已成",
    "9": "废单",
}

# 内部状态机 → PTrade 委托状态码（回测子集 {2,7,8,6,9}; 0 未报不可达）
_STATUS_CODE: dict[OrderStatus, str] = {
    OrderStatus.PENDING: "2",
    OrderStatus.PARTIALLY_FILLED: "7",
    OrderStatus.FILLED: "8",
    OrderStatus.CANCELLED: "6",
    OrderStatus.REJECTED: "9",
}
_STATUS_CODE_EXPIRED = "6"  # 当日未成交过期 ≈ 已撤（部撤见下方动态判定）
_STATUS_CODE_PART_CANCELLED = "5"  # 部分成交后过期/撤销 = 部撤


def ptrade_status_of(order: Order) -> str:
    """内部订单 → PTrade 委托状态码（部撤: 部分成交后终态, 4.7）。"""
    if order.status is OrderStatus.EXPIRED or order.status is OrderStatus.CANCELLED:
        if 0.0 < order.filled_qty < order.qty:
            return _STATUS_CODE_PART_CANCELLED
        return _STATUS_CODE_EXPIRED
    return _STATUS_CODE[order.status]


def make_ptrade_context(
    *,
    capital_base: float,
    previous_date: date | None,
    data_frequency: str = "day",
    slippage: dict[str, Any] | None = None,
    commission: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """PTradeContext 工厂（4.7 官方字段表; portfolio 由每 bar 刷新注入）。"""
    return SimpleNamespace(
        capital_base=capital_base,
        previous_date=previous_date,
        sim_params=SimpleNamespace(capital_base=capital_base, data_frequency=data_frequency),
        portfolio=None,  # 每 bar 刷新（adapter 注入）
        initialized=False,  # initialize 后置 True
        slippage=SimpleNamespace(
            volume_limit=(slippage or {}).get("volume_limit", 0.25),
            price_impact=(slippage or {}).get("price_impact", 0.0),
        ),
        commission=SimpleNamespace(
            tax=(commission or {}).get("tax", 0.0),
            cost=(commission or {}).get("cost", 0.0),
            min_trade_cost=(commission or {}).get("min_trade_cost", 5.0),
        ),
        blotter=SimpleNamespace(current_dt=None),
        recorded_vars={},
    )


def ptrade_portfolio(pf: UniformPortfolio) -> Any:
    """UniformPortfolio → PTradePortfolio（positions 值为 PTrade Position 投影）。"""
    view = ptrade_portfolio_view(pf)
    view.positions = {c: ptrade_position_view(p) for c, p in sorted(pf.positions.items())}
    view.long_positions = view.positions
    return view


def ptrade_position(pos: UniformPosition) -> Any:
    """UniformPosition → PTradePosition（字段直投, 4.7）。"""
    return ptrade_position_view(pos)


class PTradeOrder:
    """官方订单回执（4.7: amount 买正卖负, filled 带符号; entrust_no 委托号）。

    生命周期: 下单时构造（status=2 已报占位）→ `bind` 绑定引擎订单 →
    状态随内部状态机动态映射（get_orders/get_order 读取时实时计算）。
    """

    def __init__(
        self,
        *,
        entrust_no: str,
        symbol: str,
        amount: float,
        dt: datetime,
        price: float | None = None,
    ) -> None:
        self.entrust_no = entrust_no  # 委托号（回测=模拟回执 id）
        self.cancel_entrust_no: str | None = None  # 撤单号（cancel_order 后置）
        self.id = entrust_no  # 官方 id 与委托号同源
        self.symbol = symbol  # XSHG/XSHE 尾缀（4.7 Order.symbol）
        self.amount = amount  # 买正卖负
        self.dt = dt  # 下单时刻（当前 bar）
        self.created = dt  # 官方 created 同 dt
        self.limit: float | None = price  # 限价（回测市价单语义 → None）
        self.priceGear = "MARKET"  # 价格档位（回测统一市价, 已知近似）
        self._engine_order: Order | None = None  # 绑定的引擎订单（状态映射源）

    # ------------------------------------------------------------------
    def bind(self, engine_order: Order) -> None:
        """绑定引擎订单（sync_orders 时由适配器调用, 5.3.1）。"""
        self._engine_order = engine_order

    @property
    def status(self) -> str:
        """PTrade 委托状态码（0/2/5/6/7/8/9; 动态映射内部状态机）。"""
        eo = self._engine_order
        if eo is None:
            return "2"  # 已报（未受理/被忽略, 4.7 占位）
        return ptrade_status_of(eo)

    @property
    def filled(self) -> float:
        """已成交量（带符号: 买正卖负）。"""
        eo = self._engine_order
        if eo is None:
            return 0.0
        signed = eo.filled_qty if self.amount >= 0 else -eo.filled_qty
        return signed

    @property
    def filled_qty(self) -> float:
        """已成交量（绝对值, 内部视图）。"""
        return abs(self.filled)

    def __repr__(self) -> str:
        return (
            f"PTradeOrder(entrust_no={self.entrust_no!r}, symbol={self.symbol!r}, "
            f"amount={self.amount}, status={self.status}:{PTRADE_STATUS.get(self.status)})"
        )


def make_bar_data(
    *,
    symbol: str,
    dt: datetime,
    name: str = "",
    open: float = 0.0,
    close: float = 0.0,
    high: float = 0.0,
    low: float = 0.0,
    volume: float = 0.0,
    money: float = 0.0,
    preclose: float = 0.0,
    high_limit: float = 0.0,
    low_limit: float = 0.0,
    is_open: int = 1,
) -> SimpleNamespace:
    """PTradeBarData 工厂（4.7 日线全字段; 分钟字段补 0.0 归 M5, 字段位先占）。

    is_open: 停牌 0 / 正常 1（官方语义: 1=开盘可交易）。
    unlimited: 是否无涨跌停限制（ETF 跨境等; 日线默认 False）。
    """
    return SimpleNamespace(
        symbol=symbol,
        name=name,
        dt=dt,
        is_open=is_open,
        open=open,
        close=close,
        price=close,  # price=现价（日线=收盘, 官方语义）
        low=low,
        high=high,
        volume=volume,
        money=money,
        preclose=preclose,
        high_limit=high_limit,
        low_limit=low_limit,
        unlimited=False,
    )


def ptrade_symbol(code: str) -> str:
    """内部码 → PTrade 外部码（沪市 .SS / 深市 .SZ, 4.7 修订）。"""
    return denormalize_code(code, platform="ptrade")
