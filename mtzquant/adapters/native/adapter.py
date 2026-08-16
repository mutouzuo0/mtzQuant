# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:52:00
# @update_time        : 2026/08/16 21:59:08
# @description : F6 NativeAdapter：原生策略适配器（StrategyAdapter 协议首个实现, 设计 4.2/4.5）

"""NativeAdapter（设计 4.2）——StrategyAdapter 协议首个实现。

注入 initialize(context)/on_bar(context, bar) 两个入口（native 非公开平台）,
把策略下单族（order/order_target_value/order_target_quantity/order_value）
翻译为统一的 OrderRequest（4.5 归一: target_value 归一与整手取整在引擎侧完成,
适配器只做意图翻译与记账视图透传）。

M2 的 joinquant/ptrade 适配器复用本骨架（detect 规则表已在 AdapterRegistry）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mtzquant.core.errors import NotImplementedApiError
from mtzquant.engine.orders import OrderDirection, OrderRequest, OrderStyle


class NativeAdapter:
    """原生策略适配器（非公开平台, 供黄金用例/CLI 驱动引擎）。"""

    platform = "native"

    def __init__(self) -> None:
        self._ctx: SimpleNamespace = SimpleNamespace(
            g={}, adapter=self, timestamp=datetime(2000, 1, 1, 15, 0)
        )
        self._orders: list[OrderRequest] = []
        self._initialize: Callable[[Any], None] | None = None
        self._on_bar: Callable[[Any, Any], None] | None = None

    # ------------------------------------------------------------------
    # StrategyAdapter 协议
    # ------------------------------------------------------------------
    def load(self, strategy_path: Path, context: Any = None) -> None:
        """加载策略源码并解析 initialize/on_bar 两个入口。"""
        code = Path(strategy_path).read_text(encoding="utf-8")
        namespace: dict[str, Any] = {}
        exec(compile(code, str(strategy_path), "exec"), namespace)  # noqa: S102
        self._initialize = namespace.get("initialize")
        self._on_bar = namespace.get("on_bar")
        if self._initialize is None or self._on_bar is None:
            raise NotImplementedApiError(
                "on_bar",
                self.platform,
                alternative="native 策略必须定义 initialize(context) 与 on_bar(context, bar)",
            )
        if context is not None:
            self._ctx = context

    def setup(self, account_view: Any = None) -> None:
        self._ctx.account = account_view

    def on_before_trading(self, ev: Any = None) -> None:
        pass  # native v1: 无盘前钩子

    def on_bar(self, ev: Any = None) -> None:
        if self._on_bar is not None:
            self._on_bar(self._ctx, ev)

    def on_after_trading(self, ev: Any = None) -> None:
        pass  # native v1: 无盘后钩子

    def take_orders(self) -> list[OrderRequest]:
        out, self._orders = self._orders, []
        return out

    def sync_orders(self, pairs: list[tuple[OrderRequest, Any]]) -> None:
        """M2 协议扩展: native 无平台回执, 忽略（模拟回执对齐仅平台适配器需要, 4.7）。"""
        return

    def finalize(self) -> None:
        pass

    # ------------------------------------------------------------------
    # 下单族（策略脚本注入 context.adapter 调用, 4.5 归一）
    # ------------------------------------------------------------------
    def _req(
        self,
        code: str,
        direction: OrderDirection,
        style: OrderStyle,
        api: str,
        **kw: Any,
    ) -> None:
        self._orders.append(
            OrderRequest(
                code=code,
                direction=direction,
                style=style,
                order_api=api,
                created_at=getattr(self._ctx, "timestamp", datetime(2000, 1, 1, 15, 0)),
                **kw,
            )
        )

    def order(
        self, code: str, direction: OrderDirection, qty: float, *, api: str = "order"
    ) -> None:
        self._req(code, direction, OrderStyle.QUANTITY, api, quantity=qty)

    def order_value(self, code: str, value: float) -> None:
        self._req(
            code,
            OrderDirection.BUY if value >= 0 else OrderDirection.SELL,
            OrderStyle.VALUE,
            "order_value",
            value=abs(value),
        )

    def order_target_value(self, code: str, target_value: float) -> None:
        self._req(
            code,
            OrderDirection.BUY if target_value >= 0 else OrderDirection.SELL,
            OrderStyle.TARGET_VALUE,
            "order_target_value",
            target_value=abs(target_value),
        )

    def order_target_quantity(self, code: str, target_quantity: float) -> None:
        self._req(
            code,
            OrderDirection.BUY if target_quantity >= 0 else OrderDirection.SELL,
            OrderStyle.TARGET_QUANTITY,
            "order_target_quantity",
            target_quantity=abs(target_quantity),
        )


def _register() -> None:
    from mtzquant.adapters.base import register_adapter

    register_adapter("native", NativeAdapter)


_register()
