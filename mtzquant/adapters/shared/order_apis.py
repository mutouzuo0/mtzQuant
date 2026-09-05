# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:50:00
# @update_time        : 2026/08/27 15:30:00
# @description : K5 下单族归一（4.5/5.3.4）：五函数 → OrderRequest, BrokerGateway 落点 + target 去重

"""下单族归一（设计 4.5）——`order/order_target/order_value/order_target_value/order_market`。

五函数全部构造统一 `OrderRequest`（含 order_api 来源名 + created_at 取当前 bar 时刻）,
经 `BrokerGateway.submit_request` 提交（适配器内部落点 = 引擎 `take_orders` 通道）,
返回平台 Order 模拟回执（由注入的 `wrap` 回调包装; 默认返回 order_id）。

语义:
- target 意图不在适配器解析（5.3.4-②: 接受时一次性解析, 引擎 `_normalize` 已有）;
- 平台代码入参先归一（复用 core/codes.normalize_code, 不复制实现）;
- 方向由符号决定（买正卖负, 两平台官方语义一致）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from mtzquant.core.codes import normalize_code
from mtzquant.engine.orders import OrderDirection, OrderRequest, OrderStyle


@runtime_checkable
class BrokerGateway(Protocol):
    """下单落点（4.4/5.3.4: 适配器内部收集 → 引擎 take_orders 受理）。"""

    def submit_request(self, req: OrderRequest) -> str: ...  # 返回 order_id（模拟回执）


def _submit(
    gateway: BrokerGateway,
    *,
    api: str,
    code: str,
    direction: OrderDirection,
    style: OrderStyle,
    clock: Callable[[], datetime],
    **kw: Any,
) -> tuple[str, OrderRequest]:
    norm = normalize_code(code)
    req = OrderRequest(
        code=norm,
        direction=direction,
        style=style,
        order_api=api,
        created_at=clock(),
        **kw,
    )
    return gateway.submit_request(req), req


def _direction(amount: float) -> OrderDirection:
    return OrderDirection.BUY if amount >= 0 else OrderDirection.SELL


def make_order_api(
    platform: str,
    gateway: BrokerGateway,
    clock: Callable[[], datetime],
    *,
    wrap: Callable[[str, OrderRequest], Any] | None = None,
) -> Any:
    """构造策略可见下单命名空间（五函数 + order_shares 聚宽 L2 尽力）。

    `wrap(order_id, req)`: 平台 Order 模拟回执工厂（缺省返回 order_id 字符串）。
    """

    def _out(order_id: str, req: OrderRequest) -> Any:
        return wrap(order_id, req) if wrap is not None else order_id

    ns = _Namespace()

    def order(security: str, amount: float) -> Any:
        oid, req = _submit(
            gateway,
            api="order",
            code=security,
            direction=_direction(amount),
            style=OrderStyle.QUANTITY,
            clock=clock,
            quantity=abs(amount),
        )
        return _out(oid, req)

    def order_target(security: str, target_amount: float) -> Any:
        oid, req = _submit(
            gateway,
            api="order_target",
            code=security,
            direction=_direction(target_amount),
            style=OrderStyle.TARGET_QUANTITY,
            clock=clock,
            target_quantity=abs(target_amount),
        )
        return _out(oid, req)

    def order_value(security: str, value: float) -> Any:
        oid, req = _submit(
            gateway,
            api="order_value",
            code=security,
            direction=_direction(value),
            style=OrderStyle.VALUE,
            clock=clock,
            value=abs(value),
        )
        return _out(oid, req)

    def order_target_value(security: str, target_value: float) -> Any:
        oid, req = _submit(
            gateway,
            api="order_target_value",
            code=security,
            direction=_direction(target_value),
            style=OrderStyle.TARGET_VALUE,
            clock=clock,
            target_value=abs(target_value),
        )
        return _out(oid, req)

    def order_market(security: str, amount: float) -> Any:
        oid, req = _submit(
            gateway,
            api="order_market",
            code=security,
            direction=_direction(amount),
            style=OrderStyle.MARKET,
            clock=clock,
            quantity=abs(amount),
        )
        return _out(oid, req)

    def order_shares(security: str, amount: float) -> Any:
        """聚宽 L2 `order_shares`（按股数, 与 order 同义; 尽力实现, 4.6）。"""
        oid, req = _submit(
            gateway,
            api="order_shares",
            code=security,
            direction=_direction(amount),
            style=OrderStyle.QUANTITY,
            clock=clock,
            quantity=abs(amount),
        )
        return _out(oid, req)

    ns.order = order
    ns.order_target = order_target
    ns.order_value = order_value
    ns.order_target_value = order_target_value
    ns.order_market = order_market
    ns.order_shares = order_shares
    ns.platform = platform
    return ns


# target 风格集合（同 bar 同标的多次调用 → 后者覆盖前者意图）
_TARGET_STYLES = frozenset({OrderStyle.TARGET_QUANTITY, OrderStyle.TARGET_VALUE})


def dedup_target_orders(requests: list[OrderRequest]) -> list[OrderRequest]:
    """同 bar 同标的 target 订单去重：只保留最后一次 target 调用。

    平台语义: `order_target(stock, N)` / `order_target_value(stock, V)` 表达
    「把该标的调到目标量/值」的意图。同一 bar 内多次调用时，前一次意图已被
    后一次覆盖（真实平台中前一次下单后持仓/冻结变化，后续调用算出不同差额
    甚至零差额）。回测适配器在 `take_orders()` 阶段模拟此语义：同 code 多
    笔 target 请求只保留最后一条，非 target 请求（order/order_value 增量单）
    全部保留。

    算法: 反向遍历，每个 code 首次遇到的 target 订单保留（即原序最后一条），
    更早的 target 订单丢弃。非 target 订单无条件保留。O(n) 时间。
    """
    seen_target_codes: set[str] = set()
    result: list[OrderRequest] = []
    for req in reversed(requests):
        if req.style in _TARGET_STYLES:
            if req.code in seen_target_codes:
                continue  # 同 code 更早的 target 调用 → 丢弃
            seen_target_codes.add(req.code)
        result.append(req)
    result.reverse()
    return result


class _Namespace:
    """轻量命名空间（策略下单函数挂载点, 4.5）。"""

    def __init__(self) -> None:
        pass

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
