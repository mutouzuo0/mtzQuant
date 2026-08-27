# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 21:33:45
# @update_time        : 2026/08/25 23:10:00
# @description : mtzQuant: 本地优先、事件驱动、多策略平台兼容的量化研究与回测平台底座

"""mtzQuant: 本地优先、事件驱动、多策略平台兼容的量化研究与回测平台底座。

架构分层（设计文档 2.1）:
    L1 store   持久层   SQLite + SQLAlchemy（回测记录/订单/成交/指标）
    L2 data    数据层   SourceDriver → DataNormalizer → MarketDataProvider（PIT）
    L3 engine  引擎层   统一回测引擎（时间推进 + 事件循环 + BrokerSim 撮合）
    L4 server  服务层   FastAPI REST + WebSocket（M4）
    L5 web     界面层   浏览器前端（M4）
"""

from __future__ import annotations

__version__ = "0.1.4"

METRICS_VERSION = "1.0.0"  # 指标口径版本（设计 8.4，口径修订时递增）
