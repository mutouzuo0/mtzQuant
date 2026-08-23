# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:14:00
# @update_time        : 2026/08/23 13:05:00
# @description : M4-W4 FastAPI 应用：REST + WS(event_seq) + 静态页 + 认证（7 章/6.2/6.3/13.5）

"""create_app（M4-W4）——在 M2-W0 最小版上扩展（不重写）。

- `GET /` 单页监控（web/static/index.html, ECharts 本地化）;
  前端资源带版本指纹（P0-1: ?v=sha8 + no-cache, 消除新旧混跑）
- `/static/*` 静态资源
- `WS /api/ws?run_id=&last_event_seq=&bandwidth=` 多会话订阅 + resume 补帧 + 节流（W3）
- REST: backtests 提交（表单字段级中文校验, P1-2）/ runs 历史详情（metrics_note, P0-2）/
  pause·resume·stop / export / metrics 一键补算（P0-2）/ compare / fetch（覆盖语义, P2-2）/ health
  + strategies 列表/上传（新建页表单化）+ report.html 浏览（三面同权, 9.1）
- 认证（D4/13.5）: `server.auth_enabled` 时 token 校验（secrets server.tokens {token: role}）;
  watcher 对控制端点 403, operator 全权（Y1 实装）。

数据纪律（9.1）: 页面数据一律来自 DB/journal 同源查询, 禁止直连引擎内存对象。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from mtzquant.config import load_secrets, load_settings
from mtzquant.core.errors import MtzQuantError

from .sessions import BacktestSessionManager
from .ws import WsHub

logger = logging.getLogger("mtzquant.server")

_STATIC_DIR = Path(__file__).parent / "web" / "static"


# ------------------------------------------------------------------
# 前端资源版本指纹（P0-1: 消灭「新版 index.html + 旧版 app.js」混跑）
# 以 app.js/index.html 内容 sha256 前 8 位为版本号, 注入静态资源 URL `?v=` 参数。
# 资源内容一变 → URL 变 → 浏览器强制拉新, 杜绝无声失效; index 本体 no-cache 永远重校验。
# ------------------------------------------------------------------
def _asset_version() -> str:
    h = hashlib.sha256()
    for rel in ("index.html", "pages/app.js"):
        h.update(rel.encode("utf-8"))
        try:
            h.update((_STATIC_DIR / rel).read_bytes())
        except OSError:
            pass  # 文件缺失不阻断（测试/安装裁剪场景）
    return h.hexdigest()[:8]


_FRONTEND_VERSION = _asset_version()


def _render_index() -> bytes:
    """index.html 实时模板化（P0-1）: 每次请求重读文件 + 重算版本指纹——
    前端改动无需重启 serve 即可生效, 杜绝「旧 index + 新 app.js」混跑。"""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__ASSET_VERSION__", _asset_version()).encode("utf-8")


# 策略平台白名单与上传约束（新建页表单; 文件名防穿越: 仅字母/数字/中文/._- ）
_STRATEGY_PLATFORMS = ("joinquant", "ptrade", "native")
_FILENAME_RE = re.compile(r"^[\w.\-\u4e00-\u9fa5]+\.py$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

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
    # 静态页（P0-1: 资源带版本指纹, index 本体 no-cache 永远重校验）
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            content=_render_index(),
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/static", _NoCacheStaticFiles(directory=str(_STATIC_DIR)), name="static")

    logger.info(
        "前端资源版本 %s（assets ?v= 指纹: 新 index + 旧 app.js 混跑已消除）",
        _FRONTEND_VERSION,
    )

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
            runs = manager.running_runs()
            return {"mode": "serve", "running": len(runs), "runs": runs}
        if runtime is not None:
            return runtime.info()
        return {"run_id": None, "task_name": None, "status": "idle", "runs": []}

    @app.get("/api/read-task")
    async def read_task(path: str = "", dep: None = Depends(_require_any)) -> dict[str, Any]:
        """读取本地任务 JSON（新建页「读取文件」; 路径相对仓库根或绝对）。"""
        import os

        p = Path(path)
        if not p.is_absolute():
            p = Path(".") / p
        if not p.is_file() or os.path.abspath(p).startswith(os.path.abspath("data")):
            raise HTTPException(status_code=404, detail=f"任务文件不可读: {path}")
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"任务 JSON 解析失败: {exc}") from exc

    @app.post("/api/backtests")
    async def submit_backtest(
        body: dict[str, Any], dep: None = Depends(_require_operator)
    ) -> dict[str, Any]:
        if manager is None:
            raise HTTPException(status_code=501, detail="服务未装配会话管理器")
        friendly = _friendly_task_error(body)  # P1-2: 表单场景字段级中文校验（先于 pydantic）
        if friendly:
            raise HTTPException(status_code=400, detail=friendly)
        try:
            run_id = manager.submit(body)
        except MtzQuantError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except ValidationError as exc:  # 任务 JSON schema 校验失败 → 可读 400
            parts = [
                f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}"
                for e in exc.errors()
            ]
            raise HTTPException(
                status_code=400, detail=f"任务 JSON 校验失败: {'; '.join(parts)}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
        return {"run_id": run_id, "status": "running"}

    # ------------------------------------------------------------------
    # 策略文件（新建页表单化: 列表 + 上传, operator 才可写）
    # ------------------------------------------------------------------
    @app.get("/api/strategies")
    async def list_strategies(
        platform: str = "ptrade", dep: None = Depends(_require_any)
    ) -> list[dict[str, Any]]:
        """列出 strategies/<platform>/ 下策略文件（相对仓库根, 与任务 strategy.file 同源）。"""
        if platform not in _STRATEGY_PLATFORMS:
            raise HTTPException(status_code=404, detail=f"未知平台: {platform}")
        d = Path("strategies") / platform
        if not d.is_dir():
            return []
        return [
            {
                "name": p.name,
                "path": f"strategies/{platform}/{p.name}",
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(),
            }
            for p in sorted(d.glob("*.py"))
        ]

    @app.post("/api/strategies/upload")
    async def upload_strategy(
        body: dict[str, Any], dep: None = Depends(_require_operator)
    ) -> dict[str, Any]:
        """上传策略源码到 strategies/<platform>/（文件名清洗 + 语法预检 + 覆盖需显式确认）。"""
        platform = str(body.get("platform", ""))
        filename = str(body.get("filename", ""))
        code = body.get("code", "")
        if platform not in _STRATEGY_PLATFORMS:
            raise HTTPException(status_code=400, detail=f"平台须为 {'/'.join(_STRATEGY_PLATFORMS)}")
        if not _FILENAME_RE.match(filename):
            raise HTTPException(
                status_code=400, detail="文件名非法（仅字母/数字/中文/._- 且以 .py 结尾）"
            )
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(status_code=400, detail="策略代码不能为空")
        if len(code.encode("utf-8")) > 2_000_000:
            raise HTTPException(status_code=400, detail="策略文件超过 2MB 上限")
        try:
            compile(code, filename, "exec")
        except SyntaxError as exc:
            raise HTTPException(
                status_code=400, detail=f"Python 语法错误（第 {exc.lineno} 行）: {exc.msg}"
            ) from exc
        target = Path("strategies") / platform / filename
        overwritten = target.is_file()
        if overwritten and not body.get("overwrite"):
            raise HTTPException(
                status_code=409, detail=f"已存在同名文件: {target}（overwrite=true 可覆盖）"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        return {"path": f"strategies/{platform}/{filename}", "overwritten": overwritten}

    @app.get("/api/runs")
    async def list_runs(limit: int = 50, dep: None = Depends(_require_any)) -> list[dict[str, Any]]:
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        repo = RunRepo(init_db(settings.database.url))
        rows = repo.list_runs(limit=limit, include_eliminated=True)
        for r in rows:
            r["started_at"] = _iso(r.get("started_at"))
            r["finished_at"] = _iso(r.get("finished_at"))
        live = [h for h in manager.running_runs()] if manager else []
        # 会话管理器句柄（含已完成）与 DB 行合并: DB 行打底提供 platform/指标等落库字段,
        # 句柄仅叠加实时状态——原实现整行替换, Web 提交的 run 完成后句柄永远在列,
        # platform/总收益/年化恒缺（前端显示 "—"）。句柄缺 DB 行时（刚 submit）仍置顶。
        by_id = {h["run_id"]: h for h in live}
        merged: list[dict[str, Any]] = []
        for r in rows:
            h = by_id.pop(r["run_id"], None)
            if h is not None:
                r.update({k: v for k, v in h.items() if v not in (None, "")})
            merged.append(r)
        return list(by_id.values()) + merged

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

    @app.post("/api/runs/{run_id}/metrics")
    async def recompute_metrics(
        run_id: str, dep: None = Depends(_require_operator)
    ) -> dict[str, Any]:
        """P0-2 一键补算指标: 旧 run 未落库 metrics 时, 从 results/<run_id>/summary.json 回填。

        口径与引擎 persist 一致（{metrics, status}, 8.4 同源; 与 runner._metrics_json 相同结构）。
        """
        run_dir = Path("results") / run_id
        summary_path = run_dir / "summary.json"
        if not summary_path.is_file():
            raise HTTPException(
                status_code=404, detail=f"无结果产物可补算: {run_dir}/summary.json（run 未导出）"
            )
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        summary = json.loads(summary_path.read_text(encoding="utf-8")) or {}
        metrics = summary.get("metrics") or {}
        repo = RunRepo(init_db(settings.database.url))
        run = repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
        repo.set_metrics(
            run_id,
            json.dumps(
                {"metrics": metrics, "status": summary.get("status", run.status)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            str(summary.get("metrics_version") or "8.4-v1"),
        )
        return {"run_id": run_id, "recomputed": True, "metrics": metrics}

    @app.get("/api/runs/{run_id}/report")
    async def run_report(run_id: str, dep: None = Depends(_require_any)) -> FileResponse:
        """Web 端报告浏览（9.1 三面同权）: report.html 存在则直出, 缺则按需生成后直出。"""
        if not _RUN_ID_RE.match(run_id) or ".." in run_id:
            raise HTTPException(status_code=404, detail=f"run_id 非法: {run_id}")
        run_dir = Path("results") / run_id
        if not run_dir.is_dir():
            raise HTTPException(
                status_code=404, detail=f"回测产物目录不存在: {run_dir}（run 未完成或未导出）"
            )
        report = run_dir / "report.html"
        if not report.is_file():
            from mtzquant.engine.report import render_report

            try:
                render_report(run_id)
            except MtzQuantError as exc:
                raise HTTPException(status_code=404, detail=f"报告生成失败: {exc.message}") from exc
        return FileResponse(report, media_type="text/html")

    @app.get("/api/runs/{run_id}/navs")
    async def run_navs(run_id: str, dep: None = Depends(_require_any)) -> list[dict[str, Any]]:
        """每日净值明细（8.3.4; 报告/监控页全量以 DB 为准, 6.1）。"""
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        repo = RunRepo(init_db(settings.database.url))
        return repo.get_navs(run_id)

    @app.get("/api/runs/{run_id}/source")
    async def run_source(run_id: str, dep: None = Depends(_require_any)) -> dict[str, Any]:
        """查看该 run 实际运行的策略源码快照（提交时捕获, 与文件后续修改无关, P2 diff 同源）。"""
        if not _RUN_ID_RE.match(run_id) or ".." in run_id:
            raise HTTPException(status_code=404, detail=f"run_id 非法: {run_id}")
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        repo = RunRepo(init_db(settings.database.url))
        info = repo.get_snapshot_info(run_id)
        if info is None:
            raise HTTPException(status_code=404, detail=f"run 无策略快照: {run_id}")
        return {"run_id": run_id, **info}

    @app.get("/api/runs/{run_id}/logs")
    async def run_logs(
        run_id: str, limit: int = 2000, dep: None = Depends(_require_any)
    ) -> dict[str, Any]:
        """该 run 的运行日志（journal 中 log/status/progress/corp_action 事件, 附 error_log）。

        进度(progress)事件并入日志视图——「后台在干什么」逐日可见（M4, 8.3.7 同源）。
        """
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import DetailRepo, RunRepo

        repo = RunRepo(init_db(settings.database.url))
        run = repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
        entries = DetailRepo(repo.engine).journal(run_id, limit=limit)
        logs = []
        for e in entries:
            kind = e.get("type")
            if kind not in ("log", "status", "progress", "corp_action"):
                continue
            d = e.get("data") or {}
            if kind == "log":
                level = d.get("level") or d.get("kind") or "info"
                message = d.get("message") or json.dumps(d, ensure_ascii=False)
            elif kind == "progress":
                level = "info"
                message = (
                    f"进度 {d.get('day_index', '?')}/{d.get('total_days', '?')} "
                    f"({d.get('trade_date', '')}) {round((d.get('percent') or 0) * 100)}%"
                    + (f" · 已用 {d.get('elapsed_seconds')}s" if d.get("elapsed_seconds") else "")
                )
            elif kind == "status":
                level = "info"
                message = f"状态 → {d.get('status') or ''}"
            else:  # corp_action
                level = "warn"
                message = f"公司行为 {d.get('detail') or ''} @ {d.get('ex_date') or ''}"
            logs.append(
                {
                    "event_seq": e.get("event_seq"),
                    "ts": e.get("ts"),
                    "type": kind,
                    "level": level,
                    "current_dt": d.get("current_dt"),
                    "message": message,
                }
            )
        return {"run_id": run_id, "logs": logs, "error_log": run.error_log, "status": run.status}

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str, dep: None = Depends(_require_operator)) -> dict[str, Any]:
        """删除 run: 物理删除 DB 全量记录 + 移除 results/<run_id> 产物目录（永久不可恢复）。"""
        import shutil

        if manager is not None and manager.get(run_id) is not None:
            raise HTTPException(status_code=400, detail="运行中的 run 不能删除，请先在监控页终止")
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        repo = RunRepo(init_db(settings.database.url))
        run = repo.get(run_id)
        if run is None or run.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
        repo.purge_run(run_id, force=True)
        shutil.rmtree(Path("results") / run_id, ignore_errors=True)
        return {"run_id": run_id, "deleted": True}

    @app.post("/api/runs/batch-delete")
    async def batch_delete_runs(
        body: dict[str, Any], dep: None = Depends(_require_operator)
    ) -> dict[str, Any]:
        """批量物理删除 run: DB 全量记录 + results 产物目录（永久不可恢复, 历史页「批量删除」）。"""
        import shutil

        run_ids = [str(x) for x in body.get("run_ids", [])]
        if not run_ids:
            raise HTTPException(status_code=400, detail="需要 run_ids")
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        repo = RunRepo(init_db(settings.database.url))
        if manager is not None:
            running = [rid for rid in run_ids if manager.get(rid) is not None]
            if running:
                raise HTTPException(
                    status_code=400, detail="运行中的 run 不能删除: " + ", ".join(running)
                )
        deleted: list[str] = []
        missing: list[str] = []
        for rid in run_ids:
            run = repo.get(rid)
            if run is None or run.deleted_at is not None:
                missing.append(rid)
                continue
            repo.purge_run(rid, force=True)
            shutil.rmtree(Path("results") / rid, ignore_errors=True)
            deleted.append(rid)
        return {"deleted": deleted, "missing": missing}

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
        """7.6 两步下载: 检查覆盖（dry_run）→ confirm 后下载。

        P2-2: dry_run 结果带覆盖语义（covered_count/start/end + missing_days + missing_segments）,
        前端一行说清「已有什么 / 缺什么 / 将下载多少」。
        """
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
                "covered_count": r.covered_count,
                "covered_start": r.covered_start,
                "covered_end": r.covered_end,
                "missing_days": r.missing_days,
                "missing_segments": r.missing_segments,
                "reason": r.reason,
            }
            for r in reports
        ]

    @app.get("/api/health")
    def health(dep: None = Depends(_require_any)) -> dict[str, Any]:
        """3.10/7.7 数据体检（M4-Z1 成品化: DuckDB 全库扫描）。

        同步 def（非 async）: FastAPI 自动丢线程池——体检是分钟级重查询,
        async 直跑会阻塞事件循环导致整站请求超时（冒烟实测）。
        """
        from mtzquant.data.health import scan_health

        root = Path(settings.data.local_csv.root_path)
        report = scan_health(root)
        return report.to_dict()

    @app.get("/api/queue")
    async def queue_status(dep: None = Depends(_require_any)) -> dict[str, Any]:
        """参数扫描队列状态（M4-Z2）: BacktestQueue 进度 + Top 汇总（M3 衔接）。"""
        from mtzquant.optimize.queue import BacktestQueue

        q = BacktestQueue()  # 默认 state_dir=.cache/queue（queue_state.json 中断恢复）
        return {
            "done": q.done_count(),
            "total": len(q.state.tasks),
            "results": q.results(),
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


class _NoCacheStaticFiles(StaticFiles):
    """/static 静态资源: 强制 `Cache-Control: no-cache`（配合 ?v= 指纹, P0-1）。"""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":

            async def _plain(message: Any) -> None:
                await send(message)

            await super().__call__(scope, receive, _plain)
            return

        async def send_no_cache(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    h for h in message.get("headers", []) if h[0].lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await super().__call__(scope, receive, send_no_cache)


def _iso(dt: Any) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


def _friendly_task_error(task: dict[str, Any]) -> str | None:
    """P1-2: 表单提交常见缺漏 → 中文字段级提示（先于 pydantic 英文原文, 反馈与场景脱节）。"""
    strat = task.get("strategy") or {}
    if not isinstance(strat, dict) or not str(strat.get("file") or "").strip():
        return "请先选择策略文件（或上传本地 .py）"
    bt = task.get("backtest") or {}
    if not isinstance(bt, dict):
        return "backtest 配置缺失"
    start, end = str(bt.get("start") or ""), str(bt.get("end") or "")
    if not start or not end:
        return "回测起止日期不完整（YYYY-MM-DD）"
    try:
        from datetime import date as _date

        _date.fromisoformat(start)
        _date.fromisoformat(end)
    except ValueError:
        return f"日期格式应为 YYYY-MM-DD: {start} ~ {end}"
    if start >= end:
        return "开始日期须早于结束日期"
    return None


def _d(dt: Any) -> str:
    return dt.isoformat() if dt else ""
