# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:13:47
# @description : T-U04：订单状态机（设计 5.3.1 / 4.5）：合法迁移全矩阵、非法迁移、成交数学

"""T-U04：订单状态机（设计 5.3.1 / 4.5）：合法迁移全矩阵、非法迁移、成交数学。"""

from __future__ import annotations

import dataclasses
from datetime import datetime as dt

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.orders import (
    TERMINAL_STATUSES,
    TRANSITION_TABLE,
    Fill,
    Order,
    OrderDirection,
    OrderEvent,
    OrderEventType,
    OrderRequest,
    OrderStatus,
    OrderStyle,
    TimeInForce,
    transition_order,
)

T0 = dt(2026, 8, 15, 9, 30)
T1 = dt(2026, 8, 15, 10, 0)
T2 = dt(2026, 8, 15, 10, 30)
T3 = dt(2026, 8, 15, 11, 0)
T4 = dt(2026, 8, 15, 14, 0)
T5 = dt(2026, 8, 15, 15, 0)


def _order(qty: float = 100.0) -> Order:
    """构造一个已受理、状态为 pending 的测试订单（默认整手 100 股）。"""
    return Order(
        order_id="ord-1",
        run_id="run-1",
        code="510300.SH",
        side=OrderDirection.BUY,
        style=OrderStyle.QUANTITY,
        qty=qty,
        order_api="order_target_value",
        submitted_at=T0,
        eligible_fill_at=T1,
    )


def test_order_creation_defaults() -> None:
    o = _order()
    assert o.status is OrderStatus.PENDING
    assert o.filled_qty == 0.0
    assert o.remaining_qty == o.qty == 100.0
    assert o.avg_fill_price is None
    assert o.cancelled_at is None
    assert o.time_in_force is TimeInForce.DAY


def test_order_rejects_non_positive_qty() -> None:
    for bad in (0.0, -100.0, 1.0 - 100.0):
        with pytest.raises(MtzQuantError, match="必须为正"):
            _order(bad)


def test_walks_full_path_pending_partial_filled() -> None:
    """合法迁移全路径：pending → partially_filled → filled（设计 5.3.1 主链路）。"""
    o = _order(qty=200.0)
    ev1 = transition_order(o, OrderEventType.PARTIAL_FILL, event_time=T1, qty=100.0, price=3.0)
    assert o.status is OrderStatus.PARTIALLY_FILLED
    assert o.filled_qty == 100.0
    assert o.remaining_qty == 100.0
    assert o.avg_fill_price == 3.0
    assert ev1.event_type is OrderEventType.PARTIAL_FILL
    assert ev1.event_time == T1

    ev2 = transition_order(o, OrderEventType.FILL, event_time=T2, qty=100.0, price=3.2)
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == 200.0
    assert o.remaining_qty == 0.0
    # 加权平均成本 = (100×3.0 + 100×3.2) / 200 = 3.1
    assert o.avg_fill_price == pytest.approx(3.1)
    assert ev2.price == 3.2


def test_legal_transition_matrix_all_cells() -> None:
    """合法迁移全矩阵：逐一断言表中每个 (status, event) 都可以迁移到目标状态。"""
    cases: list[tuple[OrderStatus, OrderEventType, OrderStatus]] = [
        (OrderStatus.PENDING, OrderEventType.PARTIAL_FILL, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.PENDING, OrderEventType.FILL, OrderStatus.FILLED),
        (OrderStatus.PENDING, OrderEventType.CANCEL, OrderStatus.CANCELLED),
        (OrderStatus.PENDING, OrderEventType.EXPIRE, OrderStatus.EXPIRED),
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.PARTIAL_FILL, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.FILL, OrderStatus.FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.CANCEL, OrderStatus.CANCELLED),
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.EXPIRE, OrderStatus.EXPIRED),
    ]
    for from_status, event, to_status in cases:
        o = _order(qty=300.0)
        # 先迁移到 from_status（除 pending 外统一走 partial fill）
        if from_status is not OrderStatus.PENDING:
            o.status = OrderStatus.PARTIALLY_FILLED
            o.filled_qty = 100.0
            o.remaining_qty = 200.0
            o.avg_fill_price = 3.0
        qty = 0.0
        if event is OrderEventType.FILL:
            qty = o.remaining_qty
        elif event is OrderEventType.PARTIAL_FILL:
            qty = o.remaining_qty / 2
        transition_order(o, event, event_time=T1, qty=qty, price=3.5)
        assert o.status is to_status, f"{from_status.value}+{event.value} 应得 {to_status.value}"

    # 导出快照与实现保持一致（文档对照，设计 5.3.1）：可由合并贯通获得相同的独立快照粒度
    assert TRANSITION_TABLE == {
        (OrderStatus.PENDING, OrderEventType.PARTIAL_FILL): OrderStatus.PARTIALLY_FILLED,
        (OrderStatus.PENDING, OrderEventType.FILL): OrderStatus.FILLED,
        (OrderStatus.PENDING, OrderEventType.CANCEL): OrderStatus.CANCELLED,
        (OrderStatus.PENDING, OrderEventType.EXPIRE): OrderStatus.EXPIRED,
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.PARTIAL_FILL): OrderStatus.PARTIALLY_FILLED,
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.FILL): OrderStatus.FILLED,
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.CANCEL): OrderStatus.CANCELLED,
        (OrderStatus.PARTIALLY_FILLED, OrderEventType.EXPIRE): OrderStatus.EXPIRED,
    }


def test_terminal_states_reject_any_event() -> None:
    """终态（filled/cancelled/expired/rejected）拒绝一切后续事件，且订单字段保持不变。"""
    terminal = [
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
    ]
    for status in terminal:
        for event in OrderEventType:
            o = _order()
            o.status = status
            o.filled_qty = 100.0
            o.remaining_qty = 0.0
            before = (o.status, o.filled_qty, o.remaining_qty)
            with pytest.raises(MtzQuantError, match="终态"):
                transition_order(o, event, event_time=T5, qty=1.0, price=3.0)
            assert (o.status, o.filled_qty, o.remaining_qty) == before
    assert TERMINAL_STATUSES == frozenset(terminal)


def test_illegal_non_terminal_transition_rejected() -> None:
    """非终态非法迁移（本状态+事件不在矩阵内）抛异常且订单不变。"""
    # PENDING 直接受理事件 / 撤单后再次取消
    illegal: list[tuple[OrderStatus, OrderEventType]] = [
        (OrderStatus.PENDING, OrderEventType.ACCEPTED),
        # accept/reject 是受理前事件，不作用于 Order 状态机
        (OrderStatus.PENDING, OrderEventType.REJECTED),
    ]
    for status, event in illegal:
        o = _order()
        o.status = status
        with pytest.raises(MtzQuantError, match="非法状态迁移"):
            transition_order(o, event, event_time=T1, qty=0.0)
        assert o.status is status  # 订单未被篡改


def test_fill_must_consume_exact_remaining() -> None:
    """FILL 事件必须恰好填满剩余；差一分也不许（撮合层用 PARTIAL_FILL 表达不足量）。"""
    for wrong in (50.0, 0.0, 150.0):
        o = _order(qty=100.0)
        with pytest.raises(MtzQuantError):
            transition_order(o, OrderEventType.FILL, event_time=T1, qty=wrong, price=3.0)
        assert o.filled_qty == 0.0 and o.remaining_qty == 100.0


def test_partial_fill_qty_bounds() -> None:
    """PARTIAL_FILL 数量必须严格处于 (0, remaining_qty) 开区间。"""
    for bad in (0.0, -10.0, 100.0, 200.0):
        o = _order(qty=100.0)
        with pytest.raises(MtzQuantError):
            transition_order(o, OrderEventType.PARTIAL_FILL, event_time=T1, qty=bad, price=3.0)


def test_fill_price_must_be_positive() -> None:
    for price in (None, 0.0, -1.0):
        o = _order()
        with pytest.raises(MtzQuantError, match="成交价必须为正"):
            transition_order(o, OrderEventType.FILL, event_time=T1, qty=100.0, price=price)


def test_cancel_sets_timestamp_and_keeps_fill() -> None:
    """部分成交后撤单：已成交部分保留、cancelled_at 落时间戳。"""
    o = _order(qty=200.0)
    transition_order(o, OrderEventType.PARTIAL_FILL, event_time=T2, qty=120.0, price=3.0)
    ev = transition_order(o, OrderEventType.CANCEL, event_time=T4)
    assert o.status is OrderStatus.CANCELLED
    assert o.cancelled_at == T4
    assert o.filled_qty == 120.0
    assert o.remaining_qty == 80.0
    assert ev.info_json is None


def test_day_order_expiration() -> None:
    """day 单收盘未成交 → expire 事件收尾（状态机层面保证终态语义，揭露时点由调度层判）。"""
    o = _order()
    ev = transition_order(o, OrderEventType.EXPIRE, event_time=T5, info_json={"reason": "day_end"})
    assert o.status is OrderStatus.EXPIRED
    assert o.remaining_qty == 100.0  # 未成交，末日自动失效
    assert ev.info_json == {"reason": "day_end"}


def test_gtc_order_survives_across_days() -> None:
    """gtc 跨日保留：调度层不产生 expire 事件，状态机无需自动动作。"""
    o = _order()
    o.time_in_force = TimeInForce.GTC
    # 次日开盘正常成交
    t_next = dt(2026, 8, 17, 9, 31)
    transition_order(o, OrderEventType.FILL, event_time=t_next, qty=100.0, price=3.0)
    assert o.status is OrderStatus.FILLED


def test_order_event_carries_payload() -> None:
    o = _order()
    ev = transition_order(
        o,
        OrderEventType.PARTIAL_FILL,
        event_time=T1,
        qty=30.0,
        price=3.05,
        info_json={"cap_ratio": 0.3},
    )
    assert isinstance(ev, OrderEvent)
    assert ev.order_id == "ord-1"
    assert ev.qty == 30.0
    assert ev.price == 3.05
    assert ev.info_json == {"cap_ratio": 0.3}


def test_fill_aggregation_fields() -> None:
    """Fill 是撮合成交的记账载体：amount 与 total_fee 四项明细合计一致（设计 8.3.3）。"""
    f = Fill(
        order_id="ord-1",
        code="510300.SH",
        side=OrderDirection.SELL,
        price=3.2,
        volume=500.0,
        fill_time=T1,
        commission=0.16,
        stamp_tax=0.8,
        transfer_fee=0.5,
        slippage_cost=0.3,
    )
    assert f.amount == pytest.approx(1600.0)
    assert f.total_fee == pytest.approx(1.76)
    assert f.total_fee == f.commission + f.stamp_tax + f.transfer_fee + f.slippage_cost


def test_order_request_is_pure_intent() -> None:
    """OrderRequest 是意图载体：与状态机无关、不可变（frozen），四风格字段与 style 配套。"""
    req = OrderRequest(
        code="510300.SH",
        direction=OrderDirection.BUY,
        style=OrderStyle.TARGET_VALUE,
        order_api="order_target_value",
        created_at=T0,
        target_value=10_000.0,
    )
    assert isinstance(req, OrderRequest)
    assert req.target_value == 10_000.0
    assert dataclasses.is_dataclass(req)
