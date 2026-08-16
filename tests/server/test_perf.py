# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 03:30:00
# @update_time        : 2026/08/17 03:30:00
# @description : M4 性能 T-P03：日线事件端到端（引擎→WS onmessage）<200ms

"""T-P03（设计 6.1 性能预算, 12.1-M4 验收）——WS 推送路径延迟。

测法: 建立 WS 订阅（回放已入队）, 引擎线程逐事件 emit → publish_hook → WsHub
call_soon_threadsafe → 客户端 receive——测量「emit 到 onmessage」的推送延迟（日线 1 推）。
预算: 每事件 <200ms（6.1）。
"""

from __future__ import annotations

import json
import time as time_mod

from fastapi.testclient import TestClient

from mtzquant.server.app import create_app
from mtzquant.server.run_local import BacktestRuntime
from tests.fixtures.backtest_env import make_backtest_env

LATENCY_BUDGET_MS = 200.0


def test_ws_push_latency_under_budget(tmp_path) -> None:  # type: ignore[no-untyped-def]
    env = make_backtest_env(tmp_path, n=20)
    runtime = BacktestRuntime(env.task, settings=env.settings)
    app = create_app(runtime)
    latencies: list[float] = []
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as ws:
            # 预热: 消费回放（空 store 无回放）; 计时实时事件推送路径
            for i in range(1, 6):
                t0 = time_mod.perf_counter()
                runtime.store.emit(
                    "daily_nav",
                    {
                        "trade_date": f"2020-01-{i:02d}",
                        "nav": 1.0 + i / 100.0,
                        "benchmark_nav": 1.0,
                        "drawdown": 0.0,
                    },
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "daily_nav"
                latencies.append((time_mod.perf_counter() - t0) * 1000.0)
    print("WS 推送延迟(ms):", [round(x, 3) for x in latencies])
    assert max(latencies) < LATENCY_BUDGET_MS, f"WS 推送超预算: {max(latencies):.1f}ms"
