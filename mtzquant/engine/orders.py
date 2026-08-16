# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:13:47
# @description : 订单领域模型与状态机（设计 5.3.1 / 4.5）

"""订单领域模型与状态机（设计 5.3.1 / 4.5）。

订单 ≠ 成交 ≠ 拒单，三个概念彻底分离：
    OrderRequest(意图) ──validate──► Accepted(Order, 入 OpenOrderBook 待撮合)
                      └─► Rejected(仅落 order_events，不产生 Order)
    Order(pending)    → Fill / PartialFill / Cancelled / Expired
每次状态迁移产生一行 OrderEvent（审计与实盘对账基础，设计 5.3.1）。

本模块为纯领域模型（无 I/O、无 pandas），撮合/记账/绩效由 broker/account 另层负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from mtzquant.core.errors import MtzQuantError


class OrderDirection(StrEnum):
    """方向。v1 只使用 BUY/SELL；其余为期货/期权双向持仓预留（设计 1.3/4.5）。"""

    BUY = "buy"
    SELL = "sell"
    OPEN_LONG = "open_long"
    CLOSE_LONG = "close_long"
    OPEN_SHORT = "open_short"
    CLOSE_SHORT = "close_short"


class OrderStyle(StrEnum):
    """下单风格（设计 4.5：四个下单函数族归一后的语义。market 为市价单）。"""

    QUANTITY = "quantity"
    VALUE = "value"
    TARGET_QUANTITY = "target_quantity"
    TARGET_VALUE = "target_value"
    MARKET = "market"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class OrderEventType(StrEnum):
    """订单事件类型（每次状态迁移一行，设计 5.3.1）。"""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIAL_FILL = "partial_fill"
    FILL = "fill"
    CANCEL = "cancel"
    EXPIRE = "expire"


class TimeInForce(StrEnum):
    """订单有效期（设计 5.3.1：day 当日收盘未成交→Expired；gtc 跨日保留）。"""

    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"  # 预留


# 非终态的合法迁移表（M0 冻结，设计 5.3.1 订单生命周期）
_TRANSITIONS: dict[tuple[OrderStatus, OrderEventType], OrderStatus] = {
    (OrderStatus.PENDING, OrderEventType.PARTIAL_FILL): OrderStatus.PARTIALLY_FILLED,
    (OrderStatus.PENDING, OrderEventType.FILL): OrderStatus.FILLED,
    (OrderStatus.PENDING, OrderEventType.CANCEL): OrderStatus.CANCELLED,
    (OrderStatus.PENDING, OrderEventType.EXPIRE): OrderStatus.EXPIRED,
    (OrderStatus.PARTIALLY_FILLED, OrderEventType.PARTIAL_FILL): OrderStatus.PARTIALLY_FILLED,
    (OrderStatus.PARTIALLY_FILLED, OrderEventType.FILL): OrderStatus.FILLED,
    (OrderStatus.PARTIALLY_FILLED, OrderEventType.CANCEL): OrderStatus.CANCELLED,
    (OrderStatus.PARTIALLY_FILLED, OrderEventType.EXPIRE): OrderStatus.EXPIRED,
}

_TERMINAL: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED}
)


@dataclass(frozen=True)
class OrderRequest:
    """订单意图（设计 4.5：四个下单函数族 → 一种 OrderRequest）。"""

    code: str
    direction: OrderDirection
    style: OrderStyle
    order_api: str  # 来源 API 名: order_target_value 等，落库追溯
    created_at: datetime  # 回测内下单时刻
    quantity: float | None = None  # 按风格：quantity
    value: float | None = None  # value
    target_quantity: float | None = None  # target_quantity
    target_value: float | None = None  # target_value
    limit_price: float | None = None  # 限价（预留，v1 市价语义为空）


@dataclass
class Order:
    """已受理订单（设计 5.3.1 字段全集；待撮合队列 OpenOrderBook 的元素）。"""

    order_id: str
    run_id: str
    code: str
    side: OrderDirection
    style: OrderStyle
    qty: float  # 归一后数量（整手取整后）
    order_api: str
    submitted_at: datetime  # 回测内下单时刻
    status: OrderStatus = OrderStatus.PENDING
    limit_price: float | None = None
    eligible_fill_at: datetime | None = None  # 最早可撮合事件时刻（next_open → 次日开盘 bar）
    time_in_force: TimeInForce = TimeInForce.DAY
    filled_qty: float = 0.0
    remaining_qty: float = field(init=False)  # 未成交数量（部分成交核心字段）
    avg_fill_price: float | None = None
    cancelled_at: datetime | None = None
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise MtzQuantError(
                f"订单 {self.order_id!r} 数量必须为正，得到 {self.qty}",
                stage="order",
                hint="下单数量由整手取整逻辑保证为正（4.5）",
            )
        self.remaining_qty = self.qty


@dataclass(frozen=True)
class OrderEvent:
    """订单事件（每次状态迁移一行，设计 5.3.1 order_events 表）。"""

    order_id: str
    event_type: OrderEventType
    event_time: datetime
    qty: float = 0.0
    price: float | None = None
    info_json: dict[str, Any] | None = None  # 细节：拒因/一字板标记/容量截断比例等


@dataclass(frozen=True)
class Fill:
    """一笔实际成交（一订单可多笔部分成交；费用明细设计 8.3.3）。

    容量证据（8.4.4, M2-P4）: bar_volume = 成交 bar 成量, participation_rate =
    volume / bar_volume（成交参与率, 报告容量摩擦用）。
    """

    order_id: str
    code: str
    side: OrderDirection
    price: float  # raw_price 含滑点（设计 3.14：成交价唯一记账基准为 raw）
    volume: float
    fill_time: datetime
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    slippage_cost: float = 0.0
    bar_volume: float = 0.0  # 成交 bar 成量（容量证据, 8.4.4）
    participation_rate: float = 0.0  # volume / bar_volume（8.4.4）

    @property
    def amount(self) -> float:
        return self.price * self.volume

    @property
    def total_fee(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee + self.slippage_cost


def transition_order(
    order: Order,
    event_type: OrderEventType,
    *,
    event_time: datetime,
    qty: float = 0.0,
    price: float | None = None,
    info_json: dict[str, Any] | None = None,
) -> OrderEvent:
    """驱动一次状态迁移，返回对应 OrderEvent。非法迁移抛 MtzQuantError，订单保持不变。

    约束（配合 BrokerSim 的成交量裁量）:
        PARTIAL_FILL: 0 < qty < remaining_qty
        FILL:         qty == remaining_qty
        CANCEL/EXPIRE: qty 无意义（忽略）
    """
    if order.status in _TERMINAL:
        raise MtzQuantError(
            f"订单 {order.order_id!r} 已处于终态 {order.status.value}，拒绝事件 {event_type.value}",
            stage="order_state_machine",
            hint="终态订单不可再变（filled/cancelled/expired/rejected）",
        )
    new_status = _TRANSITIONS.get((order.status, event_type))
    if new_status is None:
        raise MtzQuantError(
            f"非法状态迁移: {order.status.value} + {event_type.value}",
            stage="order_state_machine",
            hint=f"当前状态 {order.status.value} 不接受 {event_type.value} 事件",
        )

    # 归一化成交数量并更新累计
    if event_type in (OrderEventType.PARTIAL_FILL, OrderEventType.FILL):
        _apply_fill(order, event_type, qty, price)
    elif event_type is OrderEventType.CANCEL:
        order.cancelled_at = event_time

    order.status = new_status
    return OrderEvent(
        order_id=order.order_id,
        event_type=event_type,
        event_time=event_time,
        qty=qty,
        price=price,
        info_json=info_json,
    )


def _apply_fill(order: Order, event_type: OrderEventType, qty: float, price: float | None) -> None:
    if event_type is OrderEventType.PARTIAL_FILL:
        if not (0.0 < qty < order.remaining_qty):
            raise MtzQuantError(
                f"partial_fill 需满足 0 < {qty} < remaining({order.remaining_qty})",
                stage="order_state_machine",
            )
    else:  # FILL
        if qty != order.remaining_qty:
            raise MtzQuantError(
                f"fill 必须填满剩余 {order.remaining_qty}，得到 {qty}",
                stage="order_state_machine",
                hint="整单成交应命中所剩数量；部分成交应使用 partial_fill 事件",
            )
    if price is None or price <= 0:
        raise MtzQuantError("成交价必须为正", stage="order_state_machine")

    # 加权平均成本（不含费用，纯成交价加权，设计 5.3.1 avg_fill_price）
    prev_cost = order.filled_qty * (order.avg_fill_price or 0.0)
    order.filled_qty += qty
    order.remaining_qty -= qty
    order.avg_fill_price = (prev_cost + qty * price) / order.filled_qty


# 状态迁移矩阵的只读导出（供测试与文档对照；模块内关联防篡改）
TRANSITION_TABLE: dict[tuple[OrderStatus, OrderEventType], OrderStatus] = dict(_TRANSITIONS)
TERMINAL_STATUSES: frozenset[OrderStatus] = _TERMINAL
