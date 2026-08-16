# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 02:40:00
# @update_time        : 2026/08/17 02:40:00
# @description : M4-W 测试 T-W01..W05：app 装配/子进程强制与控制/补帧节流/权限矩阵/committed 确认

"""T-W01..W05（设计 6.2/6.3/6.4/13.5, M4-W 服务层骨架）。"""

from __future__ import annotations

import json
import time as time_mod
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mtzquant.config import Settings
from mtzquant.server.app import create_app
from mtzquant.server.sessions import BacktestSessionManager
from mtzquant.server.ws import WsHub
from tests.fixtures.backtest_env import make_backtest_env


def _wait_terminal(manager: BacktestSessionManager, run_id: str, timeout: float = 120.0) -> str:
    """轮询 handle 直到非 running（子进程完成/异常）。"""
    t0 = time_mod.monotonic()
    while time_mod.monotonic() - t0 < timeout:
        h = manager.get(run_id)
        if h is not None and h.status != "running":
            return h.status
        time_mod.sleep(0.2)
    raise TimeoutError(f"子进程超时未终态: {run_id}")


# ============================================================
# T-W01: app 装配（REST 路由齐全, 认证关闭默认）
# ============================================================
class TestAppAssembly:
    def test_routes_and_auth_default_off(self, tmp_path: Path) -> None:
        settings = Settings()
        settings.database.url = f"sqlite:///{tmp_path / 'app.db'}"
        app = create_app(manager=None, settings=settings)
        paths = {getattr(r, "path", "") for r in app.routes}
        for expect in ("/", "/api/runtime", "/api/backtests", "/api/runs", "/api/health"):
            assert expect in paths
        with TestClient(app) as client:
            assert client.get("/api/runs").status_code == 200  # 认证默认关 → 放行
            r = client.get("/")
            assert r.status_code == 200 and "echarts.min.js" in r.text


# ============================================================
# T-W02: subprocess 强制 + 控制 + 事件桥（W5 确认）
# ============================================================
class TestSessionManager:
    def test_submit_runs_subprocess_and_journal(self, tmp_path: Path) -> None:
        env = make_backtest_env(tmp_path, n=30)
        manager = BacktestSessionManager(
            env.settings, out_root=env.out_root, db_url=env.db_url, workdir=tmp_path / "serve"
        )
        run_id = manager.submit(json.loads(env.task.model_dump_json()))
        h = manager.get(run_id)
        assert h is not None and h.proc is not None  # 子进程已 spawn（D1 强制隔离）
        status = _wait_terminal(manager, run_id)
        assert status in ("completed_exact", "completed_degraded")
        # DB 终态 + 事件日志（run_event_journal, 8.3.7）
        from sqlalchemy.orm import Session

        from mtzquant.store.models import RunEventJournal, init_db
        from mtzquant.store.repo import RunRepo

        db = init_db(env.db_url)
        run = RunRepo(db).get(run_id)
        assert run is not None and run.status == status
        with Session(db) as s:
            n = s.query(RunEventJournal).filter_by(run_id=run_id).count()
        assert n > 0  # 事件日志已落库（W5 投影落库）

    def test_control_pause_resume_stop_files(self, tmp_path: Path) -> None:
        env = make_backtest_env(tmp_path, n=30)
        manager = BacktestSessionManager(
            env.settings, out_root=env.out_root, db_url=env.db_url, workdir=tmp_path / "serve"
        )
        run_id = manager.submit(json.loads(env.task.model_dump_json()))
        manager.pause(run_id)
        ctrl = json.loads((tmp_path / "serve" / "control" / f"{run_id}.json").read_text("utf-8"))
        assert ctrl == {"pause": True, "stop": False}
        manager.resume(run_id)
        ctrl2 = json.loads((tmp_path / "serve" / "control" / f"{run_id}.json").read_text("utf-8"))
        assert ctrl2 == {"pause": False, "stop": False}
        manager.stop(run_id)
        ctrl3 = json.loads((tmp_path / "serve" / "control" / f"{run_id}.json").read_text("utf-8"))
        assert ctrl3 == {"pause": False, "stop": True}
        # 未知会话控制 → 结构化错误
        from mtzquant.core.errors import MtzQuantError

        with pytest.raises(MtzQuantError, match="未知运行会话"):
            manager.stop("nope")


# ============================================================
# T-W03: resume 补帧无缺口 + bandwidth 节流
# ============================================================
class TestWsHubM4:
    def test_resume_no_gap(self) -> None:
        """补帧 ∪ 实时 = 全量（journal 断点续传, Y2 无缺口）。"""
        import asyncio

        journal = {
            "r_1": [
                {
                    "type": "daily_nav",
                    "run_id": "r_1",
                    "event_seq": i,
                    "committed": True,
                    "data": {},
                }
                for i in range(1, 8)
            ]
        }
        hub = WsHub(
            journal_loader=lambda run_id, after: [
                e for e in journal[run_id] if e["event_seq"] > after
            ]
        )
        loop = asyncio.new_event_loop()

        async def _run() -> None:
            # 断点在 seq=4: 连接时只补 >4 的事件（无缺口续接）
            q = hub.connect(run_id="r_1", last_event_seq=4)
            got = []
            while not q.empty():
                got.append(json.loads(q.get_nowait()))
            seqs = [e["event_seq"] for e in got]
            assert seqs == [5, 6, 7]
            # 实时续接: event_seq=8 到达（await 让 call_soon_threadsafe 回调执行, 6.3）
            hub.publish_envelope(
                {"type": "status", "run_id": "r_1", "event_seq": 8, "committed": False, "data": {}}
            )
            await asyncio.sleep(0.05)
            live = json.loads(q.get_nowait())
            assert live["event_seq"] == 8  # 补帧后实时无缝续接（Y2 无缺口）

        loop.run_until_complete(_run())
        loop.close()

    def test_bandwidth_low_throttle(self) -> None:
        import asyncio

        hub = WsHub()
        loop = asyncio.new_event_loop()

        async def _run() -> None:
            q = hub.connect(run_id="r_1", bandwidth="low")
            # 实时发布: fill/log 丢弃, daily_nav 每 5 日一推
            for i in range(1, 7):
                hub.publish_envelope(
                    {
                        "type": "fill",
                        "run_id": "r_1",
                        "event_seq": i,
                        "committed": False,
                        "data": {},
                    }
                )
            for i in range(1, 12):
                hub.publish_envelope(
                    {
                        "type": "daily_nav",
                        "run_id": "r_1",
                        "event_seq": 100 + i,
                        "committed": False,
                        "data": {},
                    }
                )
            await asyncio.sleep(0.05)
            got = []
            while not q.empty():
                got.append(json.loads(q.get_nowait()))
            assert not any(e["type"] == "fill" for e in got)  # fill 丢弃
            navs = [e for e in got if e["type"] == "daily_nav"]
            assert len(navs) == 3  # 1,6,11（每 5 日）

        loop.run_until_complete(_run())
        loop.close()


# ============================================================
# T-W04: 认证权限矩阵（watcher/operator, 13.5）
# ============================================================
class TestAuthMatrix:
    def _app(self, secrets: dict, tmp_path: Path) -> TestClient:
        settings = Settings()
        settings.server.auth_enabled = True
        settings.database.url = f"sqlite:///{tmp_path / 'auth.db'}"
        app = create_app(manager=None, settings=settings, secrets=secrets)
        return TestClient(app)

    def test_permission_matrix(self, tmp_path: Path) -> None:
        secrets = {
            "server": {
                "tokens": {
                    "op-token": "operator",
                    "watch-token": "watcher",
                }
            }
        }
        client = self._app(secrets, tmp_path)
        # 无 token → 401
        assert client.get("/api/runs").status_code == 401
        # watcher 可读, 控制 403
        assert (
            client.get("/api/runs", headers={"Authorization": "Bearer watch-token"}).status_code
            == 200
        )
        r = client.post("/api/backtests", json={}, headers={"Authorization": "Bearer watch-token"})
        assert r.status_code == 403  # watcher 无权控制
        # operator 通过认证（manager=None → 501, 非 401/403）
        r2 = client.post("/api/backtests", json={}, headers={"Authorization": "Bearer op-token"})
        assert r2.status_code == 501


# ============================================================
# T-W05: committed 确认升级（W5）——子进程事件流 → hub fan-out → 终态
# ============================================================
class TestCommittedConfirmation:
    def test_stream_fanout_to_hub(self, tmp_path: Path) -> None:
        """子进程流式事件经 hub fan-out（committed:false 即时发; committed 确认终态）。"""
        import asyncio

        env = make_backtest_env(tmp_path, n=30)
        hub = WsHub()
        manager = BacktestSessionManager(
            env.settings,
            hub=hub,
            out_root=env.out_root,
            db_url=env.db_url,
            workdir=tmp_path / "serve",
        )
        loop = asyncio.new_event_loop()
        received: list[dict] = []

        async def _run() -> None:
            q = hub.connect(run_id=None)  # 全量订阅
            run_id = manager.submit(json.loads(env.task.model_dump_json()))
            # 读事件直到 committed 确认
            deadline = time_mod.monotonic() + 120
            got_committed = False
            while time_mod.monotonic() < deadline:
                if q.empty():
                    await asyncio.sleep(0.1)
                    continue
                msg = json.loads(q.get_nowait())
                received.append(msg)
                if msg["type"] == "committed":
                    got_committed = True
                    break
            assert got_committed, f"未收到 committed 确认（收到 {len(received)} 条）"
            assert any(e["type"] == "daily_nav" for e in received)  # 实时事件已 fan-out
            assert received[-1]["committed"] is True  # committed 轻量确认（W5）
            return run_id

        run_id = loop.run_until_complete(_run())
        loop.close()
        # 终态落定
        assert manager.get(run_id).status in ("completed_exact", "completed_degraded")
