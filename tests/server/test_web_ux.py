# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/18 23:30:00
# @update_time        : 2026/08/18 23:45:00
# @description : M4 Web 易用性端点测试——策略列表/上传、report 按需生成、KPI 列、非法任务 400

"""M4 Web 易用性（新建表单 / 历史 KPI / Web 报告）服务端测试。

- strategies 端点: GET 列表 + POST 上传（文件名防穿越、compile 语法预检、409 覆盖确认、watcher 403）
- report 端点: results/<run_id>/report.html 存在直出 / 缺则按需生成 / 产物缺失 404 / run_id 防穿越
- /api/runs: LEFT JOIN metrics 附 sharpe/total_return/annual_return/max_drawdown(.value)（8.4 同源）
- POST /api/backtests: pydantic 校验失败 → 400 可读 detail（原为 500）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mtzquant.config import Settings
from mtzquant.server.app import create_app
from mtzquant.store.models import init_db
from mtzquant.store.repo import RunRepo

_GOOD_CODE = "def initialize(context):\n    g.x = 1\n"


def _client(
    tmp_path: Path, *, auth: bool = False, secrets: dict[str, Any] | None = None
) -> TestClient:
    settings = Settings()
    settings.database.url = f"sqlite:///{tmp_path / 'ux.db'}"
    if auth:
        settings.server.auth_enabled = True
    app = create_app(manager=None, settings=settings, secrets=secrets)
    return TestClient(app)


# ============================================================
# 策略文件: 列表 + 上传生命周期
# ============================================================
class TestStrategiesEndpoints:
    def test_list_and_upload_lifecycle(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)  # strategies/ 以 cwd 为根 → 隔离到 tmp, 不污染仓库
        client = _client(tmp_path)
        assert client.get("/api/strategies?platform=ptrade").json() == []  # 空目录

        # 上传 → 落盘 + 列表可见
        r = client.post(
            "/api/strategies/upload",
            json={"platform": "ptrade", "filename": "s1.py", "code": _GOOD_CODE},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"path": "strategies/ptrade/s1.py", "overwritten": False}
        saved = tmp_path / "strategies" / "ptrade" / "s1.py"
        assert saved.read_text(encoding="utf-8") == _GOOD_CODE
        lst = client.get("/api/strategies?platform=ptrade").json()
        assert [x["name"] for x in lst] == ["s1.py"]

        # 同名未确认 → 409; overwrite=true → 覆盖成功
        r2 = client.post(
            "/api/strategies/upload",
            json={"platform": "ptrade", "filename": "s1.py", "code": "x = 2\n"},
        )
        assert r2.status_code == 409
        r3 = client.post(
            "/api/strategies/upload",
            json={"platform": "ptrade", "filename": "s1.py", "code": "x = 2\n", "overwrite": True},
        )
        assert r3.status_code == 200 and r3.json()["overwritten"] is True

        # 语法错误 → 400（行号提示）
        r4 = client.post(
            "/api/strategies/upload",
            json={"platform": "ptrade", "filename": "bad.py", "code": "def (:\n"},
        )
        assert r4.status_code == 400 and "语法" in r4.json()["detail"]

        # 文件名穿越/非法 → 400; 平台白名单外 → 400（GET → 404）
        for bad in ("../evil.py", "a/b.py", "x.txt", ""):
            rb = client.post(
                "/api/strategies/upload",
                json={"platform": "ptrade", "filename": bad, "code": "x = 1\n"},
            )
            assert rb.status_code == 400, bad
        assert (
            client.post(
                "/api/strategies/upload",
                json={"platform": "evil", "filename": "ok.py", "code": "x = 1\n"},
            ).status_code
            == 400
        )
        assert client.get("/api/strategies?platform=evil").status_code == 404

    def test_watcher_cannot_upload(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        secrets = {"server": {"tokens": {"op-token": "operator", "watch-token": "watcher"}}}
        client = _client(tmp_path, auth=True, secrets=secrets)
        body = {"platform": "ptrade", "filename": "s.py", "code": _GOOD_CODE}
        # watcher 可读列表但无权上传（13.5 操作面）
        assert (
            client.get(
                "/api/strategies?platform=ptrade", headers={"Authorization": "Bearer watch-token"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/strategies/upload", json=body, headers={"Authorization": "Bearer watch-token"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/strategies/upload", json=body, headers={"Authorization": "Bearer op-token"}
            ).status_code
            == 200
        )


# ============================================================
# report.html: 按需生成 + 直出 + 404
# ============================================================
class TestReportEndpoint:
    def test_report_on_demand_and_cache(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)  # results/ 以 cwd 为根
        client = _client(tmp_path)
        run_dir = tmp_path / "results" / "r_test_001"
        run_dir.mkdir(parents=True)

        r = client.get("/api/runs/r_test_001/report")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert (run_dir / "report.html").is_file()  # 首次访问已生成缓存
        assert client.get("/api/runs/r_test_001/report").status_code == 200  # 二次直出

        # 产物目录不存在 / run_id 含路径穿越 → 404
        assert client.get("/api/runs/r_nope/report").status_code == 404
        assert client.get("/api/runs/x..y/report").status_code == 404


# ============================================================
# /api/runs: KPI 指标列（metrics LEFT JOIN, 8.4 同源）
# ============================================================
class TestRunsListKpi:
    def test_kpi_columns_from_metrics(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'kpi.db'}"
        repo = RunRepo(init_db(db_url))
        snap, _ = repo.get_or_create_snapshot(file_name="s.py", code_text="x=1", sha256="h" * 8)
        repo.create_run(
            run_id="r_kpi_1",
            task_name="t",
            platform="ptrade",
            snapshot_id=snap.id,
            params_json="{}",
        )
        repo.set_metrics(
            "r_kpi_1",
            json.dumps(
                {
                    "metrics": {
                        "sharpe": 1.5,
                        "total_return": 0.3,
                        "annual_return": 0.2,
                        "max_drawdown": {
                            "value": 0.1,
                            "peak_date": "2020-01-01",
                            "trough_date": "2020-02-01",
                        },
                    }
                }
            ),
            "8.4-v1",
        )
        settings = Settings()
        settings.database.url = db_url
        app = create_app(manager=None, settings=settings)
        rows = TestClient(app).get("/api/runs").json()
        row = next(r for r in rows if r["run_id"] == "r_kpi_1")
        assert row["sharpe"] == 1.5
        assert row["total_return"] == 0.3
        assert row["annual_return"] == 0.2
        assert row["max_drawdown"] == 0.1  # dict → .value 扁平化

        # 无 metrics 的 run（运行中）→ 四列 None（前端显示 "—"）
        repo.create_run(
            run_id="r_kpi_2",
            task_name="t2",
            platform="native",
            snapshot_id=snap.id,
            params_json="{}",
        )
        rows2 = TestClient(app).get("/api/runs").json()
        row2 = next(r for r in rows2 if r["run_id"] == "r_kpi_2")
        assert row2["sharpe"] is None and row2["max_drawdown"] is None


# ============================================================
# P0-1 静态资源版本指纹: index 带 ?v= 且 no-cache, 消灭新旧混跑
# ============================================================
class TestAssetVersioning:
    def test_index_has_asset_fingerprint_and_no_cache(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        # 版本指纹已注入（占位符被替换, 子资源 URL 带 ?v=）
        assert "__ASSET_VERSION__" not in html
        assert "/static/pages/app.js?v=" in html
        assert "/static/vendor/echarts.min.js?v=" in html
        assert r.headers.get("cache-control") == "no-cache"

    def test_static_served_with_no_cache(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        r = client.get("/static/pages/app.js")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache"

    def test_frontend_version_logged_on_startup(self, tmp_path: Path, caplog) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="mtzquant.server"):
            _client(tmp_path)  # create_app 装配即打一行前端版本日志
        assert any("前端资源版本" in rec.message for rec in caplog.records)


# ============================================================
# P0-2 metrics_note: 已完成但指标缺失 → 给出原因; 补算端点
# ============================================================
class TestMetricsNote:
    def _seed_run(self, db_url: str, run_id: str, status: str) -> None:
        repo = RunRepo(init_db(db_url))
        snap, _ = repo.get_or_create_snapshot(file_name="s.py", code_text="x=1", sha256="h" * 8)
        repo.create_run(
            run_id=run_id,
            task_name="t",
            platform="ptrade",
            snapshot_id=snap.id,
            params_json="{}",
            status=status,
        )

    def test_completed_without_metrics_has_note(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'note.db'}"
        self._seed_run(db_url, "r_no_metrics", "completed_degraded")
        settings = Settings()
        settings.database.url = db_url
        rows = TestClient(create_app(manager=None, settings=settings)).get("/api/runs").json()
        row = next(r for r in rows if r["run_id"] == "r_no_metrics")
        assert row["total_return"] is None
        assert "未写入绩效指标" in row["metrics_note"]

    def test_running_has_waiting_note(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'note2.db'}"
        self._seed_run(db_url, "r_running", "running")
        settings = Settings()
        settings.database.url = db_url
        rows = TestClient(create_app(manager=None, settings=settings)).get("/api/runs").json()
        row = next(r for r in rows if r["run_id"] == "r_running")
        assert "运行中" in row["metrics_note"]

    def test_with_metrics_no_note(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'note3.db'}"
        self._seed_run(db_url, "r_ok", "completed_exact")
        repo = RunRepo(init_db(db_url))
        repo.set_metrics(
            "r_ok",
            json.dumps({"metrics": {"sharpe": 1.0, "total_return": 0.1}}, sort_keys=True),
            "8.4-v1",
        )
        settings = Settings()
        settings.database.url = db_url
        rows = TestClient(create_app(manager=None, settings=settings)).get("/api/runs").json()
        row = next(r for r in rows if r["run_id"] == "r_ok")
        assert row["metrics_note"] is None


class TestRecomputeMetrics:
    def test_recompute_from_summary_json(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)  # results/ 以 cwd 为根
        db_url = f"sqlite:///{tmp_path / 'rc.db'}"
        repo = RunRepo(init_db(db_url))
        snap, _ = repo.get_or_create_snapshot(file_name="s.py", code_text="x=1", sha256="h" * 8)
        repo.create_run(
            run_id="r_old",
            task_name="t",
            platform="ptrade",
            snapshot_id=snap.id,
            params_json="{}",
            status="completed_degraded",
        )
        run_dir = tmp_path / "results" / "r_old"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": "r_old",
                    "status": "completed_degraded",
                    "metrics_version": "8.4-v1",
                    "metrics": {"sharpe": 2.1, "total_return": 0.5, "max_drawdown": {"value": 0.2}},
                }
            ),
            encoding="utf-8",
        )
        settings = Settings()
        settings.database.url = db_url
        app = create_app(manager=None, settings=settings)
        client = TestClient(app)
        r = client.post("/api/runs/r_old/metrics")
        assert r.status_code == 200, r.text
        assert r.json()["recomputed"] is True
        rows = client.get("/api/runs").json()
        row = next(x for x in rows if x["run_id"] == "r_old")
        assert row["sharpe"] == 2.1 and row["total_return"] == 0.5
        assert row["max_drawdown"] == 0.2  # dict → .value 扁平化

    def test_recompute_without_summary_404(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        client = _client(tmp_path)
        assert client.post("/api/runs/r_nope/metrics").status_code == 404


# ============================================================
# P2-2 /api/fetch 覆盖语义透传（dry_run → covered/missing 字段）
# ============================================================
class TestFetchCoverageSemantics:
    def test_dry_run_returns_coverage_fields(self, tmp_path: Path) -> None:
        import pandas as pd

        data_root = tmp_path / "data"
        kline = data_root / "kline" / "etf" / "day" / "510300.SH.csv"
        kline.parent.mkdir(parents=True)
        df = pd.DataFrame(
            [
                {
                    "ts_code": "510300.SH",
                    "trade_date": d,
                    "open": "3.5",
                    "high": "3.6",
                    "low": "3.4",
                    "close": "3.55",
                    "vol": "1",
                    "amount": "1",
                }
                for d in ("20240102", "20240103", "20240104", "20240105")
            ]
        )
        df.to_csv(kline, index=False)

        settings = Settings()
        settings.data.local_csv.root_path = str(data_root)
        app = create_app(manager=None, settings=settings)
        r = TestClient(app).post(
            "/api/fetch",
            json={
                "codes": ["510300.SH"],
                "start": "2024-01-01",
                "end": "2024-01-31",
                "confirm": False,
            },
        )
        assert r.status_code == 200, r.text
        row = r.json()[0]
        assert row["status"] == "dry_run"
        assert row["covered_count"] == 4
        assert row["covered_start"] == "2024-01-02" and row["covered_end"] == "2024-01-05"
        assert row["missing_days"] == 19  # 23 个工作日 − 已有 4
        assert row["missing_segments"]  # 含单日段（01-01）与区间段
        assert "2024-01-01~2024-01-01" not in row["missing_segments"]


# ============================================================
# 非法任务 → 400 可读（原 ValidationError 500）
# ============================================================
class TestSubmitValidation:
    def test_invalid_task_returns_400(self, tmp_path: Path) -> None:
        from mtzquant.server.sessions import BacktestSessionManager

        settings = Settings()
        settings.database.url = f"sqlite:///{tmp_path / 'submit.db'}"
        manager = BacktestSessionManager(settings, workdir=tmp_path / "serve")
        app = create_app(manager=manager, settings=settings)
        r = TestClient(app).post("/api/backtests", json={"task_name": "x"})  # 缺必填字段
        assert r.status_code == 400
        # P1-2: 表单场景字段级中文校验（先于 pydantic 英文原文）
        assert "请先选择策略文件" in r.json()["detail"]


# ============================================================
# 监控会话列表 + 源码/日志/删除（M4: 会话选择 + 历史操作）
# ============================================================
class _StubManager:
    """最小桩: 仅暴露 /api/runtime 与删除判定所需接口。"""

    hub = None

    def __init__(self, runs: list[dict[str, Any]] | None = None, handle: Any = None) -> None:
        self._runs = runs or []
        self._handle = handle

    def running_runs(self) -> list[dict[str, Any]]:
        return self._runs

    def get(self, run_id: str) -> Any:
        return self._handle


class TestMonitorOps:
    def test_runtime_returns_runs_list(self, tmp_path: Path) -> None:
        settings = Settings()
        settings.database.url = f"sqlite:///{tmp_path / 'rt.db'}"
        mgr = _StubManager(
            runs=[
                {
                    "run_id": "r_live_1",
                    "task_name": "轮动V16",
                    "status": "running",
                    "started_at": "2026-08-23T10:00:00",
                    "finished_at": None,
                    "error": "",
                }
            ]
        )
        app = create_app(manager=mgr, settings=settings)
        j = TestClient(app).get("/api/runtime").json()
        assert j["running"] == 1
        assert j["runs"][0]["task_name"] == "轮动V16"
        assert j["runs"][0]["started_at"].startswith("2026-08-23")

    def test_empty_universe_passes_friendly_validation(self, tmp_path: Path) -> None:
        # manager=None: 合法 task（不填 universe）越过友好校验, 落到 501 而非 400
        client = _client(tmp_path)
        r = client.post(
            "/api/backtests",
            json={
                "task_name": "x",
                "strategy": {"file": "strategies/ptrade/s.py", "type": "ptrade"},
                "backtest": {"start": "2024-01-01", "end": "2024-01-31"},
            },
        )
        assert r.status_code == 501
        assert "会话管理器" in r.json()["detail"]


class TestRunSourceLogsDelete:
    def _seed(self, tmp_path: Path, run_id: str = "r_ops_1") -> RunRepo:
        db_url = f"sqlite:///{tmp_path / 'ops.db'}"
        repo = RunRepo(init_db(db_url))
        snap, _ = repo.get_or_create_snapshot(
            file_name="strategies/ptrade/s.py",
            code_text="g.symbols = ['510300.SH']\n",
            sha256="a" * 64,
        )
        repo.create_run(
            run_id=run_id,
            task_name="t",
            platform="ptrade",
            snapshot_id=snap.id,
            params_json="{}",
        )
        return repo

    def _client(self, tmp_path: Path) -> TestClient:
        settings = Settings()
        settings.database.url = f"sqlite:///{tmp_path / 'ops.db'}"
        return TestClient(create_app(manager=None, settings=settings))

    def test_source_endpoint(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        client = self._client(tmp_path)
        j = client.get("/api/runs/r_ops_1/source").json()
        assert j["code"] == "g.symbols = ['510300.SH']\n"
        assert j["file_name"] == "strategies/ptrade/s.py"
        assert j["line_count"] == 2  # count("\n")+1
        assert client.get("/api/runs/r_nope/source").status_code == 404
        assert client.get("/api/runs/x..y/source").status_code == 404

    def test_logs_endpoint(self, tmp_path: Path) -> None:
        repo = self._seed(tmp_path)
        from mtzquant.store.repo import DetailRepo

        detail = DetailRepo(repo.engine)
        detail.insert_journal(
            [
                {
                    "run_id": "r_ops_1",
                    "event_seq": 1,
                    "kind": "log",
                    "committed": True,
                    "ts": 1,
                    "payload_json": json.dumps({"level": "info", "message": "hello"}),
                },
                {
                    "run_id": "r_ops_1",
                    "event_seq": 2,
                    "kind": "daily_nav",
                    "committed": True,
                    "ts": 2,
                    "payload_json": json.dumps({"nav": 1.0}),
                },
                {
                    "run_id": "r_ops_1",
                    "event_seq": 3,
                    "kind": "status",
                    "committed": True,
                    "ts": 3,
                    "payload_json": json.dumps({"status": "completed_exact", "terminal": True}),
                },
            ]
        )
        client = self._client(tmp_path)
        j = client.get("/api/runs/r_ops_1/logs").json()
        assert [row["message"] for row in j["logs"]] == ["hello", "状态 → completed_exact"]
        assert len(j["logs"]) == 2  # daily_nav 不入日志; status 生命周期入列
        assert j["logs"][1]["type"] == "status"
        assert client.get("/api/runs/r_nope/logs").status_code == 404

    def test_delete_run(self, tmp_path: Path, monkeypatch) -> None:
        self._seed(tmp_path)
        run_dir = tmp_path / "results" / "r_ops_1"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text("{}", encoding="utf-8")
        monkeypatch.chdir(tmp_path)  # results/ 以 cwd 为根
        client = self._client(tmp_path)
        r = client.delete("/api/runs/r_ops_1")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert not run_dir.exists()  # 产物目录已移除
        assert all(x["run_id"] != "r_ops_1" for x in client.get("/api/runs").json())
        assert client.delete("/api/runs/r_ops_1").status_code == 404

    def test_delete_running_refused(self, tmp_path: Path) -> None:
        settings = Settings()
        settings.database.url = f"sqlite:///{tmp_path / 'ops.db'}"
        mgr = _StubManager(handle=object())
        app = create_app(manager=mgr, settings=settings)
        r = TestClient(app).delete("/api/runs/r_ops_1")
        assert r.status_code == 400
        assert "运行中" in r.json()["detail"]
