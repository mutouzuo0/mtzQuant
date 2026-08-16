# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:50:00
# @update_time        : 2026/08/16 21:59:08
# @description : F6 适配器协议 + AdapterRegistry（设计 4.2/4.3）：策略平台插拔核心

"""适配器协议与注册表（设计 4.2/4.3）。

StrategyAdapter 协议 —— 各平台适配器（native/joinquant/ptrade/qmt）统一实现,
引擎/CLI 通过 AdapterRegistry 按 strategy.type 选用。

依赖契约（import-linter）: mtzquant.adapters 禁止 import engine 内部
（broker/account/metrics）——适配器只翻译策略意图为 OrderRequest, 撮合/记账/绩效
只有引擎侧一份实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mtzquant.core.errors import MtzQuantError


@runtime_checkable
class StrategyAdapter(Protocol):
    """策略适配器协议（设计 4.2）。"""

    platform: str  # native / joinquant / ptrade / qmt

    def load(self, strategy_path: Path, context: Any) -> None: ...
    def setup(self, account_view: Any) -> None: ...
    def on_before_trading(self, ev: Any) -> None: ...
    def on_bar(self, ev: Any) -> None: ...
    def on_after_trading(self, ev: Any) -> None: ...
    def take_orders(self) -> list[Any]: ...  # 返回 OrderRequest 列表
    def sync_orders(self, pairs: list[tuple[Any, Any]]) -> None: ...  # M2: 回执 id 对齐
    def finalize(self) -> None: ...


class AdapterRegistry:
    """平台适配器注册表（设计 4.3）。"""

    def __init__(self) -> None:
        self._adapters: dict[str, type[StrategyAdapter]] = {}

    def register(self, platform: str, cls: type[StrategyAdapter]) -> None:
        if not platform or not isinstance(platform, str):
            raise MtzQuantError(f"平台名必须为非空字符串: {platform!r}", stage="adapter_registry")
        self._adapters[platform] = cls

    def create(self, platform: str, **config: Any) -> StrategyAdapter:
        cls = self._adapters.get(platform)
        if cls is None:
            known = ", ".join(sorted(self._adapters)) or "（空）"
            raise MtzQuantError(
                f"未知策略平台适配器: {platform!r}",
                stage="adapter_registry",
                hint=f"已注册: {known}; strategy.type 检查（设计 4.3）",
            )
        return cls(**config)  # type: ignore[call-arg]

    def platforms(self) -> list[str]:
        return sorted(self._adapters)

    def detect(self, strategy_code: str) -> str | None:
        """嗅探平台（4.3 detect 规则表; 返回平台名或 None）。"""
        if "def initialize(" in strategy_code and (
            "handle_data" in strategy_code or "run_daily" in strategy_code
        ):
            return "joinquant" if "handle_data" in strategy_code else "ptrade"
        if "def initialize(" in strategy_code:
            return "native"
        return None


_default_registry = AdapterRegistry()


def register_adapter(platform: str, cls: type[StrategyAdapter]) -> None:
    _default_registry.register(platform, cls)


def create_adapter(platform: str, **config: Any) -> StrategyAdapter:
    return _default_registry.create(platform, **config)
