# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:14:00
# @update_time        : 2026/08/16 21:59:08
# @description : W0-2 最小 FastAPI：GET / 单页 + WS /api/ws（6.3 信封）+ /api/runtime

"""create_app（设计 7 章引言裁剪, M2-W0）——最小 Web 监控应用。

- `GET /`            单页监控（web/static/index.html, ECharts 本地化）
- `/static/*`        静态资源（vendor/echarts.min.js）
- `GET /api/runtime` 只读运行时状态（run_id/status/进度; W0「看」范围, 无控制面）
- `WS /api/ws`       事件流: 连接先回放已发布记录（committed 快照）再续接实时（6.3）

范围硬约束（D7）: 单页面 / 无认证（仅 127.0.0.1）/ 无暂停终止控制 / 无移动端;
信封字段与事件类型按 M4 冻结接口实现, M4 只扩不重写。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .run_local import BacktestRuntime

_STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(runtime: BacktestRuntime | None = None) -> FastAPI:
    """装配 FastAPI 应用（runtime 可注入: 测试传构建好的, 生产由 run_serve 传已启动的）。"""
    app = FastAPI(title="mtzquant monitor", version="0.1.0", docs_url=None, redoc_url=None)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/api/runtime")
    async def runtime_info() -> dict[str, Any]:
        if runtime is None:
            return {"run_id": None, "task_name": None, "status": "idle"}
        return runtime.info()

    @app.websocket("/api/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if runtime is None or runtime.hub is None:
            await ws.accept()
            await ws.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "run_id": None,
                        "ts": 0,
                        "event_seq": 0,
                        "committed": False,
                        "data": {
                            "message": "no runtime; use: mtzquant serve "
                            "--with-task configs/xxx.json"
                        },
                    },
                    ensure_ascii=False,
                )
            )
            await ws.close()
            return
        await ws.accept()
        queue = runtime.hub.connect()
        # 纯服务端推送（W0 只读监控, 入站忽略）; 断开在下次 send 时以 WebSocketDisconnect 暴露。
        # 事件队列为空时的长时间空闲断开检测（心跳/resume 断点续传）归 M4-W3。
        try:
            while True:
                msg = await queue.get()
                await ws.send_text(msg)
        except WebSocketDisconnect:
            return
        except RuntimeError:
            return
        finally:
            runtime.hub.disconnect(queue)

    return app
