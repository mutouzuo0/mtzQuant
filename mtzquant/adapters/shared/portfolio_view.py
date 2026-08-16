# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:48:00
# @update_time        : 2026/08/16 21:59:08
# @description : K4 账户只读视图 + 双平台投影（4.4 投影表）

"""账户只读视图与平台投影（设计 4.4 投影表）。

- `UniformPosition` / `UniformPortfolio`: 由 `AccountView`（session 注入的只读账户视图）
  归一出的统一结构（T+1: closeable = total - today）。
- 平台投影: `jq_position_view / ptrade_position_view / jq_portfolio_view /
  ptrade_portfolio_view` 按平台字段名逐 property 映射（只读 SimpleNamespace, 4.4）。

纪律: 本模块不碰撮合/记账（import-linter 契约）; 只做「账户视图 → 平台字段」翻译,
清仓后 positions 移除语义由账户侧保证（Account.apply_fill 已实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from mtzquant.adapters.shared.code_style import denormalize_code


@dataclass(frozen=True)
class UniformPosition:
    """统一持仓（投影源）。"""

    code: str
    total_qty: float
    today_qty: float
    avg_cost: float
    last_price: float
    market_value: float

    @property
    def closeable_qty(self) -> float:
        """T+1 可卖 = total - today（4.4）。"""
        return max(0.0, self.total_qty - self.today_qty)

    @property
    def price(self) -> float:
        return self.last_price

    @property
    def value(self) -> float:
        return self.market_value


@dataclass(frozen=True)
class UniformPortfolio:
    """统一账户视图（投影源）。"""

    positions: dict[str, UniformPosition] = field(default_factory=dict)
    available_cash: float = 0.0
    receivable_cash: float = 0.0
    frozen_cash: float = 0.0
    total_value: float = 0.0
    initial_cash: float = 0.0

    @property
    def total_cash(self) -> float:
        return self.available_cash + self.receivable_cash + self.frozen_cash

    @property
    def starting_cash(self) -> float:
        return self.initial_cash

    @property
    def positions_value(self) -> float:
        return self.total_value - self.total_cash


def uniform_portfolio(account_view: Any) -> UniformPortfolio:
    """AccountView → UniformPortfolio（缺失字段兜底, 适配多平台注入形态）。"""
    positions: dict[str, UniformPosition] = {}
    for code, pos in getattr(account_view, "positions", {}).items():
        positions[code] = UniformPosition(
            code=code,
            total_qty=float(getattr(pos, "total_qty", 0.0)),
            today_qty=float(getattr(pos, "today_qty", 0.0)),
            avg_cost=float(getattr(pos, "avg_cost", 0.0)),
            last_price=float(getattr(pos, "last_price", 0.0)),
            market_value=float(getattr(pos, "market_value", 0.0)),
        )
    return UniformPortfolio(
        positions=positions,
        available_cash=float(getattr(account_view, "available_cash", 0.0)),
        receivable_cash=float(getattr(account_view, "receivable_cash", 0.0)),
        frozen_cash=float(getattr(account_view, "frozen_cash", 0.0)),
        total_value=float(getattr(account_view, "total_value", 0.0)),
        initial_cash=float(getattr(account_view, "initial_cash", 0.0)),
    )


# ------------------------------------------------------------------
# 平台投影（4.4 投影表; 只读 SimpleNamespace, 字段名与官方一致）
# ------------------------------------------------------------------
def jq_position_view(pos: UniformPosition) -> SimpleNamespace:
    """聚宽 Position 投影（security/amount/total_amount/closeable_amount/avg_cost/price/value）。"""
    return SimpleNamespace(
        security=pos.code,
        amount=pos.total_qty,
        total_amount=pos.total_qty,
        closeable_amount=pos.closeable_qty,
        avg_cost=pos.avg_cost,
        price=pos.price,
        value=pos.value,
        # 兼容字段（聚宽常见读取）
        sid=pos.code,
        last_price=pos.last_price,
    )


def jq_portfolio_view(pf: UniformPortfolio) -> SimpleNamespace:
    """聚宽 Portfolio 投影（starting_cash/available_cash/positions_value/total_assets/…）。

    positions 键 = 平台外部码（600156.XSHG, 4.4 投影表）; `positions` 与官方一致
    为长仓字典（A 股日线无融券, short 恒空, 4.6 已知近似）。
    """
    long_positions = {denormalize_code(c): jq_position_view(p) for c, p in pf.positions.items()}
    return SimpleNamespace(
        starting_cash=pf.starting_cash,
        available_cash=pf.available_cash,
        positions_value=pf.positions_value,
        total_assets=pf.total_value,
        total_value=pf.total_value,
        market_cap=pf.total_value,  # 日线近似（无盘口股本, 4.6 已知近似）
        positions=long_positions,  # 官方入口 context.portfolio.positions[security]
        long_positions=long_positions,
        short_positions={},  # A 股日线无融券（已知近似）
        daily_returns=0.0,  # 日收益由引擎指标给出, 4.6 近似
    )


def ptrade_position_view(pos: UniformPosition) -> SimpleNamespace:
    """PTrade Position 投影（sid/amount/enable_amount/cost_basis/last_sale_price/today_amount）。"""
    return SimpleNamespace(
        sid=pos.code,
        amount=pos.total_qty,
        enable_amount=pos.closeable_qty,
        cost_basis=pos.avg_cost,
        last_sale_price=pos.last_price,
        today_amount=pos.today_qty,
        business_type="stock",
        update_time=None,
    )


def ptrade_portfolio_view(pf: UniformPortfolio) -> SimpleNamespace:
    """PTrade Portfolio 投影（portfolio_value/capital_used/returns/pnl/…）。"""
    total_value = pf.total_value
    pnl = total_value - pf.starting_cash
    return SimpleNamespace(
        portfolio_value=total_value,
        capital_used=pf.positions_value,  # 已投入市值（近似）
        returns=(total_value / pf.starting_cash - 1.0) if pf.starting_cash else 0.0,
        pnl=pnl,
        starting_cash=pf.starting_cash,
        available_cash=pf.available_cash,
        positions_value=pf.positions_value,
        market_value=pf.positions_value,
        cash=pf.total_cash,
    )
