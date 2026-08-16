# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:12:00
# @update_time        : 2026/08/16 21:59:08
# @description : W0-4 本地会话桥：BacktestSession → 事件 fan-out 到 WS（5.6）

"""BacktestRuntime（设计 5.6 裁剪, M2-W0）——「mtzquant serve --with-task」的本地桥。

装配链路与 runner.run_task 一致（CsvSourceDriver → DataCache → MarketDataProvider →
TradeCalendar → BacktestSession → UnifiedBacktestEngine）; 差异点:
- ResultStore 只挂 publish_hook（WS fan-out, 8.7 直发不经 WriteBuffer）, 不落库;
- 回测在独立线程驱动, 事件经 WsHub 跨线程推到 uvicorn 事件循环;
- 单会话单 run（多 run 并发归 M4）; 回测结束终态经 status 事件呈现。

`mtzquant serve --with-task configs/xxx.json` → run_serve: 装配+启动 runtime, 再起 uvicorn。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from mtzquant.config import Settings, load_settings
from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.engine import UnifiedBacktestEngine
from mtzquant.engine.results import ResultStore
from mtzquant.engine.runner import _settings_fees, build_pipeline, make_run_id
from mtzquant.engine.session import BacktestSession, TaskConfig

from .ws import WsHub


class BacktestRuntime:
    """本地回测运行时: 装配 + 线程驱动 + WS 桥（单会话单 run）。"""

    def __init__(
        self,
        task: TaskConfig,
        *,
        settings: Settings,
        run_id: str | None = None,
    ) -> None:
        self.task = task
        self.settings = settings
        self.run_id = run_id or make_run_id(json.loads(task.model_dump_json()))
        self._thread: threading.Thread | None = None
        self._snapshot: Any = None
        self._error: str | None = None
        self.status = "idle"
        self.total_days = 0
        self.done_days = 0
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        pipeline = build_pipeline(self.settings, self.task.universe)
        self._pipeline = pipeline
        # 事件流: publish 直发 WS（8.7 与落库解耦, W0 不做持久化）
        self.hub = WsHub()
        self.store = ResultStore(run_id=self.run_id, publish_hook=self.hub.publish)
        self.hub.attach(self.store)
        self._session = BacktestSession(
            self.task,
            driver=pipeline.driver,
            provider=pipeline.provider,
            calendar=pipeline.calendar,
            run_id=self.run_id,
            settings_fees=_settings_fees(self.settings),
            max_participation=self.settings.engine.max_participation,
            result_store=self.store,
        )
        self._engine = UnifiedBacktestEngine(self._session, broker=self._session.broker)
        self.total_days = len(self._session.trading_days())

    def start(self) -> None:
        """后台线程驱动回测（WS 订阅者不受阻塞, 8.7 直发语义）。"""
        self.status = "running"
        self._thread = threading.Thread(
            target=self._run, name="mtzquant-serve-backtest", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self._snapshot = self._engine.run()
            self.done_days = self._engine.daily_nav_rows
            self.status = self._engine.status
        except MtzQuantError as exc:
            self.status = "error"
            self._error = exc.message
        except Exception as exc:  # noqa: BLE001 — 页面可见终态, 不让线程裸退
            self.status = "error"
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            # 终态落定 committed:true（重连回放见完整快照, 6.3）
            self.store.flush()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        """只读运行时状态（GET /api/runtime, W0「看」范围）。"""
        return {
            "run_id": self.run_id,
            "task_name": self.task.task_name,
            "status": self.status,
            "total_days": self.total_days,
            "done_days": self.done_days,
            "error": self._error,
        }

    @property
    def snapshot(self) -> Any:
        return self._snapshot


def load_task(task_path: str | Path) -> TaskConfig:
    """加载任务 JSON（校验走 TaskConfig.model_validate, 与 CLI validate 同源）。"""
    p = Path(task_path)
    if not p.is_file():
        raise MtzQuantError(
            f"任务文件不存在: {p}", stage="serve", hint="--with-task 传 configs/xxx.json 路径"
        )
    return TaskConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))


def run_serve(
    *,
    with_task: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8501,
    settings: Settings | None = None,
) -> None:
    """`mtzquant serve`: 装配 runtime（可选 --with-task 启动回测）+ 起 uvicorn（仅本地可信）。"""
    from mtzquant.server.app import create_app

    settings = settings or load_settings()
    runtime: BacktestRuntime | None = None
    if with_task:
        task = load_task(with_task)
        runtime = BacktestRuntime(task, settings=settings)
        runtime.start()
    app = create_app(runtime)
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
