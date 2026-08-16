# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:14:00
# @update_time        : 2026/08/17 02:30:00
# @description : M4-W4 FastAPI 应用：REST + WS(event_seq) + 静态页 + 认证（7 章/6.2/6.3/13.5）

"""create_app（M4-W4）——在 M2-W0 最小版上扩展（不重写）。

- `GET /` 单页监控（web/static/index.html, ECharts 本地化）
- `/static/*` 静态资源
- `WS /api/ws?run_id=&last_event_seq=&bandwidth=` 多会话订阅 + resume 补帧 + 节流（W3）
- REST: backtests 提交 / runs 历史详情 / pause·resume·stop / export / compare / fetch / health
- 认证（D4/13.5）: `server.auth_enabled` 时 token 校验（secrets server.tokens {token: role}）;
  watcher 对控制端点 403, operator 全权（Y1 实装）。

数据纪律（9.1）: 页面数据一律来自 DB/journal 同源查询, 禁止直连引擎内存对象。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mtzquant.config import load_secrets, load_settings
from mtzquant.core.errors import MtzQuantError

from .sessions import BacktestSessionManager
from .ws import WsHub

_STATIC_DIR = Path(__file__).parent / "web" / "static"

# 控制端点（watcher 角色 403, operator 全权, 13.5）
_CONTROL_METHODS = {"POST"}
_CONTROL_PATHS_PREFIX = ("/api/backtests", "/api/runs/")


def create_app(
    runtime: Any | None = None,
    manager: BacktestSessionManager | None = None,
    *,
    settings: Any | None = None,
    secrets: dict[str, Any] | None = None,
) -> FastAPI:
    """装配 FastAPI 应用。

    manager: M4 会话管理器（Web 模式强制子进程, D1）;
    runtime: W0 单会话桥（legacy, 测试/本地 --with-task 用）。
    """
    settings = settings or load_settings()
    secrets = secrets if secrets is not None else load_secrets()
    hub: WsHub | None = manager.hub if manager is not None else None
    if runtime is not None:
        hub = getattr(runtime, "hub", None)

    app = FastAPI(title="mtzquant", version="0.1.0", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------
    # 静态页
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # 认证（D4/13.5, Y1）: 关 → 全部放行; 开 → /api/* 需 Bearer token（角色两档）
    # ------------------------------------------------------------------
    auth_enabled = bool(settings.server.auth_enabled)
    tokens: dict[str, str] = dict((secrets.get("server") or {}).get("tokens") or {})

    def _require_operator(authorization: str | None = Header(default=None)) -> None:
        _check_auth(authorization, require_operator=True)

    def _require_any(authorization: str | None = Header(default=None)) -> None:
        _check_auth(authorization, require_operator=False)

    def _check_auth(authorization: str | None, *, require_operator: bool) -> None:
        if not auth_enabled:
            return
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        role = tokens.get(token)
        if role is None:
            raise HTTPException(status_code=401, detail="未认证（缺 token 或无效）")
        if require_operator and role != "operator":
            raise HTTPException(status_code=403, detail="watcher 角色无权控制（13.5）")

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------
    @app.get("/api/runtime")
    async def runtime_info(dep: None = Depends(_require_any)) -> dict[str, Any]:
        if manager is not None:
            return {"mode": "serve", "running": len(manager.running_runs())}
        if runtime is not None:
            return runtime.info()
        return {"run_id": None, "task_name": None, "status": "idle"}

    @app.post("/api/backtests")
    async def submit_backtest(
        body: dict[str, Any], dep: None = Depends(_require_operator)
    ) -> dict[str, Any]:
        if manager is None:
            raise HTTPException(status_code=501, detail="服务未装配会话管理器")
        try:
            run_id = manager.submit(body)
        except MtzQuantError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/runs")
    async def list_runs(limit: int = 50, dep: None = Depends(_require_any)) -> list[dict[str, Any]]:
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        repo = RunRepo(init_db(settings.database.url))
        rows = repo.list_runs(limit=limit, include_eliminated=True)
        for r in rows:
            r["started_at"] = _iso(r.get("started_at"))
            r["finished_at"] = _iso(r.get("finished_at"))
        running = [h for h in manager.running_runs()] if manager else []
        known = {r["run_id"] for r in rows}
        return running + [r for r in rows if r["run_id"] not in known]

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str, dep: None = Depends(_require_any)) -> dict[str, Any]:
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        repo = RunRepo(init_db(settings.database.url))
        run = repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
        manifest = repo.get_manifest(run_id)
        return {
            "run_id": run.id,
            "task_name": run.task_name,
            "platform": run.platform,
            "status": run.status,
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "error_log": run.error_log,
            "eliminated_reason": run.eliminated_reason,
            "params": repo.get_params(run_id),
            "manifest": json.loads(manifest[0]) if manifest else None,
            "metrics": repo.get_metrics(run_id),
        }

    @app.post("/api/runs/{run_id}/pause")
    async def pause(run_id: str, dep: None = Depends(_require_operator)) -> dict[str, Any]:
        if manager is None:
            raise HTTPException(status_code=501, detail="服务未装配会话管理器")
        manager.pause(run_id)
        return {"run_id": run_id, "status": "paused"}

    @app.post("/api/runs/{run_id}/resume")
    async def resume(run_id: str, dep: None = Depends(_require_operator)) -> dict[str, Any]:
        if manager is None:
            raise HTTPException(status_code=501, detail="服务未装配会话管理器")
        manager.resume(run_id)
        return {"run_id": run_id, "status": "running"}

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str, dep: None = Depends(_require_operator)) -> dict[str, Any]:
        if manager is None:
            raise HTTPException(status_code=501, detail="服务未装配会话管理器")
        manager.stop(run_id)
        return {"run_id": run_id, "status": "stopping"}

    @app.get("/api/runs/{run_id}/export")
    async def run_export(run_id: str, dep: None = Depends(_require_any)) -> dict[str, Any]:
        """9.1 产物（results/<run_id> 文件清单 + report.html 路径）。"""
        run_dir = Path("results") / run_id
        if not run_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"导出目录不存在: {run_dir}")
        files = sorted(
            {
                f.name: f.stat().st_size
                for f in run_dir.iterdir()
                if f.is_file() and f.name != "report.html"
            }
        )
        return {"run_id": run_id, "dir": str(run_dir), "files": files, "report": "report.html"}

    @app.get("/api/runs/{run_id}/compare")
    async def compare(
        run_id: str, ids: str = "", dep: None = Depends(_require_any)
    ) -> dict[str, Any]:
        """多 run 指标对照 + 净值对齐（10.3; 复用 engine.compare）。"""
        from mtzquant.engine.compare import build_compare_table
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        run_ids = [run_id] + [x.strip() for x in ids.split(",") if x.strip()]
        repo = RunRepo(init_db(settings.database.url))
        metrics = [(rid, repo.get_metrics(rid)) for rid in run_ids]
        table = build_compare_table(metrics)
        return {"runs": table["runs"], "rows": table["rows"], "best": table["best"]}

    @app.post("/api/fetch")
    async def fetch(
        body: dict[str, Any], dep: None = Depends(_require_operator)
    ) -> list[dict[str, Any]]:
        """7.6 两步下载: 检查覆盖（dry_run）→ confirm 后下载。"""
        from mtzquant.data.fetcher import DataFetcher

        codes = [str(c) for c in body.get("codes", [])]
        start, end = body.get("start"), body.get("end")
        if not codes or not start or not end:
            raise HTTPException(status_code=400, detail="需要 codes/start/end")
        from datetime import date

        fetcher = DataFetcher(settings.data.local_csv.root_path)
        confirm = bool(body.get("confirm", False))
        reports = fetcher.fetch(
            codes,
            date.fromisoformat(str(start)),
            date.fromisoformat(str(end)),
            dry_run=not confirm,
        )
        return [
            {
                "code": r.code,
                "status": r.status,
                "added_rows": r.added_rows,
                "range": [r.merged_start, r.merged_end],
                "reason": r.reason,
            }
            for r in reports
        ]

    @app.get("/api/health")
    async def health(dep: None = Depends(_require_any)) -> dict[str, Any]:
        """3.10/7.7 数据体检（Z1 成品化; 此处返回覆盖摘要骨架）。"""
        from mtzquant.data.coverage import CoverageChecker

        root = Path(settings.data.local_csv.root_path)
        kline = root / "kline"
        codes: list[str] = []
        if kline.is_dir():
            for t in ("etf", "stock"):
                d = kline / t / "day"
                if d.is_dir():
                    codes += [f.stem for f in sorted(d.glob("*.csv"))]
        covs = [
            CoverageChecker(root, instrument_type="etf").coverage(c) for c in sorted(codes)[:500]
        ]
        return {
            "instruments": len(codes),
            "coverage": [
                {"code": c.code, "count": c.count, "min": _d(c.min_dt), "max": _d(c.max_dt)}
                for c in covs
            ],
        }

    # ------------------------------------------------------------------
    # WS /api/ws（W3: 多会话 + resume 补帧 + bandwidth 节流）
    # ------------------------------------------------------------------
    @app.websocket("/api/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if hub is None:
            await ws.accept()
            await ws.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "run_id": None,
                        "ts": 0,
                        "event_seq": 0,
                        "committed": False,
                        "data": {"message": "no hub; use: mtzquant serve"},
                    },
                    ensure_ascii=False,
                )
            )
            await ws.close()
            return
        await ws.accept()
        run_id = ws.query_params.get("run_id")
        last_seq = int(ws.query_params.get("last_event_seq", "0") or 0)
        bandwidth = ws.query_params.get("bandwidth", "high")
        queue = hub.connect(run_id=run_id, last_event_seq=last_seq, bandwidth=bandwidth)
        try:
            while True:
                msg = await queue.get()
                await ws.send_text(msg)
        except WebSocketDisconnect:
            return
        except RuntimeError:
            return
        finally:
            hub.disconnect(queue)

    return app


def _iso(dt: Any) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


def _d(dt: Any) -> str:
    return dt.isoformat() if dt else ""
