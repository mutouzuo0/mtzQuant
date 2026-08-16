# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:54:00
# @update_time        : 2026/08/16 21:59:08
# @description : Native 桥接适配器（设计 4.2）：StrategyAdapter 协议首个实现（F6）

"""Native 桥接适配器（设计 4.2）——StrategyAdapter 协议首个实现。

供黄金用例与 CLI 驱动引擎; M2 的 joinquant/ptrade 适配器复用本骨架。
导入本包即注册 'native' 到 AdapterRegistry（4.3 一行注册）。
"""

from __future__ import annotations

from mtzquant.adapters.native.adapter import NativeAdapter

__all__ = ["NativeAdapter"]
