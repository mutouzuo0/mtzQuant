# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:55:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-E09：NativeAdapter 注入用例 + AdapterRegistry 注册/detect（设计 4.2/4.3/4.5）

"""T-E09：NativeAdapter 注入用例（initialize/on_bar/下单族归一）与 AdapterRegistry。"""

from __future__ import annotations

import pytest

from mtzquant.adapters.base import AdapterRegistry, create_adapter
from mtzquant.adapters.native import NativeAdapter
from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.orders import OrderRequest, OrderStyle

STRATEGY = """
# coding:utf-8
def initialize(context):
    context.g["ready"] = True

def on_bar(context, bar):
    context.adapter.order_target_value("510300.SH", 100_000)
"""


def test_native_adapter_load_and_inject(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "strat.py"
    p.write_text(STRATEGY, encoding="utf-8")
    a = NativeAdapter()
    a.load(p)
    a.on_bar({"g": {}, "adapter": a})
    orders = a.take_orders()
    assert len(orders) == 1
    assert isinstance(orders[0], OrderRequest)
    assert orders[0].code == "510300.SH"
    assert orders[0].style is OrderStyle.TARGET_VALUE
    assert orders[0].order_api == "order_target_value"


def test_native_adapter_missing_entry_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "bad.py"
    p.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(MtzQuantError):
        NativeAdapter().load(p)


def test_adapter_registry_register_create() -> None:
    reg = AdapterRegistry()
    reg.register("native", NativeAdapter)
    inst = reg.create("native")
    assert isinstance(inst, NativeAdapter)
    assert inst.platform == "native"
    with pytest.raises(MtzQuantError):
        reg.create("nope")


def test_detect_rules() -> None:
    reg = AdapterRegistry()
    assert reg.detect("def initialize(c):\n    handle_data(c, None)") == "joinquant"
    assert reg.detect("def initialize(c):\n    run_daily(f, '09:30')") == "ptrade"
    assert reg.detect("def initialize(c):\n    pass") == "native"
    assert reg.detect("no entry here") is None


def test_default_registry_has_native() -> None:
    inst = create_adapter("native")
    assert inst.platform == "native"
