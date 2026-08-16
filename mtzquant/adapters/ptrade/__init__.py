# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 21:33:45
# @update_time        : 2026/08/16 21:59:08
# @description : PTrade 适配器(设计4.7)：官方策略零改动回测; 导入本包即注册 ptrade

"""PTrade 适配器(设计4.7)——官方字段投影 + L0 API 注入 + run_daily 调度语义。

导入本包即注册 'ptrade' 到 AdapterRegistry（4.3 一行注册）。
"""

from __future__ import annotations

from mtzquant.adapters.ptrade.adapter import PTradeAdapter

__all__ = ["PTradeAdapter"]
