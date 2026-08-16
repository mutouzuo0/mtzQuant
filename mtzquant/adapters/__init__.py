# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 21:33:45
# @update_time        : 2026/08/16 21:59:08
# @description : 策略平台适配层(设计第4章)：协议 + 注册表; 导入本包注册全平台适配器

"""策略平台适配层(设计第4章)：StrategyAdapter 协议 + AdapterRegistry。

导入本包即注册全部已交付平台适配器（4.3 一行注册）:
native(F6) / ptrade(M2-L) / joinquant(M2-N)。
"""

from __future__ import annotations

from mtzquant.adapters import joinquant, native, ptrade  # noqa: F401  # 注册（4.3）

__all__ = ["native", "ptrade", "joinquant"]
