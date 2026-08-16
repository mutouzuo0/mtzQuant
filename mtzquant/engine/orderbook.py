# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:00:00
# @update_time        : 2026/08/16 21:59:08
# @description : F1 OpenOrderBook：受理/冻结/eligible 筛选/更新/当日过期（设计 5.3.1/5.3.4）

"""OpenOrderBook（设计 5.3.1/5.3.4）——引擎待撮合队列。

职责:
  accept     受理: 买入冻结可用资金（预估额含佣）; 现金不足 → REJECTED（不冻结不进场）
  eligible   按 bar_dt 筛选 eligible_fill_at <= dt 的可撮合订单
  release    成交/过期/撤销后释放冻结
  day 过期   time_in_force=day 当日未成交 → EXPIRE（收市清算阶段, 5.3.4）
  cancel     客户撤销（预留）

冻结账目: _frozen[order_id] = 预估额; 与 Account.frozen_cash 同步（5.3.4 冻结/释放）。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.orders import (
    Order,
    OrderDirection,
    OrderEvent,
    OrderEventType,
    OrderStatus,
    TimeInForce,
    transition_order,
)


class OpenOrderBook:
    """待撮合订单簿（主循环单线程使用）。"""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}  # order_id → 订单
        self._frozen: dict[str, float] = {}  # order_id → 冻结现金（买入预估, 5.3.4）

    # ------------------------------------------------------------------
    def accept(
        self,
        order: Order,
        *,
        available_cash: float,
        ref_price: float,
        commission_rate: float = 0.0001,
        min_commission: float = 5.0,
    ) -> OrderEvent | None:
        """受理订单: 现金预检（买入）→ 冻结 → 进场; 不足 → REJECTED（不进场）。

        返回受理事件或拒单事件; 拒单时订单终态 REJECTED、无冻结。
        """
        if order.order_id in self._orders:
            raise MtzQuantError(
                f"订单 {order.order_id!r} 重复受理", stage="orderbook", hint="order_id 全局唯一"
            )
        if order.side in (OrderDirection.BUY, OrderDirection.CLOSE_SHORT, OrderDirection.OPEN_LONG):
            est = self._estimate_freeze(order, ref_price, commission_rate, min_commission)
            if est > available_cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "insufficient_cash"
                return OrderEvent(
                    order_id=order.order_id,
                    event_type=OrderEventType.REJECTED,
                    event_time=order.submitted_at,
                    info_json={"reason": "insufficient_cash", "available_cash": available_cash},
                )
            self._frozen[order.order_id] = est
        order.status = OrderStatus.PENDING
        self._orders[order.order_id] = order
        return OrderEvent(
            order_id=order.order_id,
            event_type=OrderEventType.ACCEPTED,
            event_time=order.submitted_at,
        )

    @staticmethod
    def _estimate_freeze(
        order: Order, ref_price: float, commission_rate: float, min_commission: float
    ) -> float:
        amount = order.qty * ref_price
        fee = max(min_commission, commission_rate * amount)
        return amount + fee

    # ------------------------------------------------------------------
    def eligible(self, dt: datetime, code: str | None = None) -> Iterator[Order]:
        """按当前事件时刻筛出可撮合订单（eligible_fill_at <= dt, 5.3.1）。

        code 非空时仅筛该标的的订单——撮合阶段⑤按标的逐 bar 驱动, 防止
        多标的池下订单被其他标的的 bar 错误撮合（阶段⑤按 code 过滤, 5.3.2）。
        """
        for order in list(self._orders.values()):
            if order.status is not OrderStatus.PENDING:
                continue
            if code is not None and order.code != code:
                continue
            if order.eligible_fill_at is None or order.eligible_fill_at <= dt:
                yield order

    def active_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.PENDING]

    def frozen_of(self, order_id: str) -> float:
        return self._frozen.get(order_id, 0.0)

    def release(self, order_id: str) -> float:
        """释放并返回该订单冻结额（成交/过期/撤销后调用, 5.3.4）。"""
        return self._frozen.pop(order_id, 0.0)

    def cancel(self, order_id: str, *, when: datetime) -> OrderEvent | None:
        """撤销待成交订单（day 单在当日撮合前/gtc 跨日均可）。"""
        order = self._orders.get(order_id)
        if order is None or order.status is not OrderStatus.PENDING:
            return None
        ev = transition_order(order, OrderEventType.CANCEL, event_time=when)
        order.cancelled_at = when
        self.release(order_id)
        return ev

    def expire_day_orders(self, *, when: datetime) -> list[OrderEvent]:
        """收盘清算: 当日已 eligible（eligible_fill_at <= when）仍未成交的 day 单 → EXPIRE。

        次日才 eligible 的订单不在此列（5.3.4 会话语义）; gtc 跨日保留。
        """
        events: list[OrderEvent] = []
        for order in self.active_orders():
            if order.time_in_force is not TimeInForce.DAY:
                continue
            if order.eligible_fill_at is not None and order.eligible_fill_at > when:
                continue  # 尚未到可撮合时点（次日单）, 今日收盘不处理
            ev = transition_order(
                order, OrderEventType.EXPIRE, event_time=when, info_json={"time_in_force": "day"}
            )
            events.append(ev)
            self.release(order.order_id)
        return events

    def drop_order(self, order_id: str) -> None:
        """订单离开账本（成交终态后清理, 释放已由 broker 处理）。"""
        self._orders.pop(order_id, None)

    def __len__(self) -> int:
        return len(self._orders)

    def debug_state(self) -> dict[str, Any]:
        return {"orders": len(self._orders), "frozen": dict(self._frozen)}
