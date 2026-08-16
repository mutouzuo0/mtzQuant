# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 02:20:00
# @update_time        : 2026/08/17 02:20:00
# @description : M4-W2 BacktestSessionManager：subprocess 强制隔离 + 控制 + 事件桥（设计 6.4/2.4）

"""回测会话管理器（设计 6.4/2.4, M4-W2）——CLI/Python API/REST 三调用面唯一入口。

- `submit(task_json)`: 预建 run（backtest_run status=running）→ **spawn 子进程**
  （`mtzquant run --stream --control`; 环境清洁 worker.clean_env + 超时进程树, 2.4）;
- 事件桥（W5）: 子进程 stdout 6.3 信封逐行 → WsHub fan-out（committed:false 即时发）;
  投影批次落库由 worker 自身（persist=True, 8.7）; 收到 committed 确认 → 终态落定;
- 控制（6.4）: pause/resume/stop 写控制文件, worker 每 bar 边界读取（engine control_refresh）;
- Web 模式强制子进程（D1: serve 父进程永不 import 策略代码）; 同 CLI `--isolate` 通道。

超时: 默认 1h（2.4）; 超时 taskkill 进程树; 退出钩子清理全部子进程。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time as time_mod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mtzquant.config import Settings, sanitize_params
from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.runner import make_run_id
from mtzquant.engine.session import TaskConfig
from mtzquant.server.ws import WsHub
from mtzquant.store.models import RUN_RUNNING, init_db
from mtzquant.store.repo import RunRepo
from mtzquant.worker.isolate import DEFAULT_TIMEOUT_SECONDS, clean_env

DEFAULT_WORKDIR = ".cache/serve"


@dataclass
class RunHandle:
    """一次运行会话（子进程 + 控制 + 状态, 6.4）。"""

    run_id: str
    task_name: str
    proc: Any = None
    control_path: Path = None  # type: ignore[assignment]
    status: str = RUN_RUNNING
    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    finished_at: datetime | None = None
    error: str = ""
    task_path: Path = None  # type: ignore[assignment]

    def info(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_name": self.task_name,
            "status": self.status,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": (
                self.finished_at.isoformat(timespec="seconds") if self.finished_at else None
            ),
            "error": self.error,
        }


class BacktestSessionManager:
    """回测会话管理器（三调用面唯一入口, 6.4/2.4）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        hub: Any | None = None,
        out_root: Path | str = "results",
        db_url: str | None = None,
        workdir: Path | str = DEFAULT_WORKDIR,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._out_root = Path(out_root)
        self._db_url = db_url or settings.database.url
        self._workdir = Path(workdir)
        self._timeout = timeout
        self._handles: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        # 事件枢纽（W3 resume 补帧源 = run_event_journal, 8.3.7）
        self.hub = hub if hub is not None else WsHub(journal_loader=self._journal)
        if hub is not None and getattr(hub, "_journal_loader", None) is None:
            hub._journal_loader = self._journal  # type: ignore[attr-defined]

    def _journal(self, run_id: str, after_seq: int) -> list[dict[str, Any]]:
        """从 run_event_journal 按 seq 回放（W3 resume 补帧; 失败返回空, 不阻断）。"""
        try:
            from mtzquant.store.repo import DetailRepo

            return DetailRepo(init_db(self._db_url)).journal(run_id, after_seq)
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # 提交（Web=强制子进程, D1）
    # ------------------------------------------------------------------
    def submit(self, task_json: dict[str, Any]) -> str:
        """提交回测任务; 返回 run_id（spawn 子进程 + 预建 running 行）。"""
        task = TaskConfig.model_validate(task_json)
        run_id = make_run_id(task_json)
        self._workdir.mkdir(parents=True, exist_ok=True)
        (self._workdir / "tasks").mkdir(exist_ok=True)
        (self._workdir / "control").mkdir(exist_ok=True)
        task_path = self._workdir / "tasks" / f"{run_id}.json"
        task_path.write_text(json.dumps(task_json, ensure_ascii=False, indent=2), encoding="utf-8")
        control_path = self._workdir / "control" / f"{run_id}.json"
        control_path.write_text(json.dumps({"pause": False, "stop": False}), encoding="utf-8")

        # 预建 running 行（REST 历史即时可见; worker persist 终态覆写, 8.3.1）
        self._precreate_run(task, run_id)

        cmd = [
            str(sys.executable),
            "-m",
            "mtzquant",
            "run",
            "-c",
            str(task_path),
            "--stream",
            "--control",
            str(control_path),
            "--run-id",
            run_id,
        ]
        # 子进程沿用 serve 进程的 settings（MTZQUANT_SETTINGS → 同数据根/同库, 2.4 环境清洁）
        env = clean_env()
        settings_path = self._workdir / "settings.json"
        settings_path.write_text(self._settings.model_dump_json(), encoding="utf-8")
        env["MTZQUANT_SETTINGS"] = str(settings_path)
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        handle = RunHandle(
            run_id=run_id,
            task_name=task.task_name,
            proc=proc,
            control_path=control_path,
            task_path=task_path,
        )
        with self._lock:
            self._handles[run_id] = handle
        threading.Thread(
            target=self._read_events, args=(handle,), name=f"serve-{run_id}", daemon=True
        ).start()
        return run_id

    # ------------------------------------------------------------------
    # 控制（6.4: pause/resume/stop, Web/CLI 同权）
    # ------------------------------------------------------------------
    def pause(self, run_id: str) -> None:
        self._write_control(run_id, {"pause": True, "stop": False})

    def resume(self, run_id: str) -> None:
        self._write_control(run_id, {"pause": False, "stop": False})

    def stop(self, run_id: str) -> None:
        self._write_control(run_id, {"pause": False, "stop": True})

    def _write_control(self, run_id: str, data: dict[str, bool]) -> None:
        handle = self._handles.get(run_id)
        if handle is None:
            raise MtzQuantError(
                f"未知运行会话: {run_id}", stage="serve", hint="该 run 不在本服务会话中"
            )
        handle.control_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    def running_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [h.info() for h in self._handles.values()]

    # ------------------------------------------------------------------
    # 事件桥（W5: 子进程 6.3 信封 → WsHub fan-out; committed 确认终态）
    # ------------------------------------------------------------------
    def _read_events(self, handle: RunHandle) -> None:
        proc = handle.proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    env = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if env.get("type") == "committed":
                    handle.status = env.get("data", {}).get("status", handle.status)
                if self.hub is not None:
                    # 全部事件（含 committed 确认）fan-out——客户端据此识别终态, 6.3
                    self.hub.publish_envelope(env)
            proc.wait(timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - 读流异常兜底（进程终态仍落定）
            handle.error = f"{type(exc).__name__}: {exc}"
        finally:
            # 关闭管道（防 ResourceWarning; stderr 已由 Popen 缓冲, 不阻塞回收）
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
            self._finalize(handle)

    def _finalize(self, handle: RunHandle) -> None:
        handle.finished_at = datetime.now().astimezone()
        if handle.status == RUN_RUNNING:
            handle.status = "error"  # 未收到 committed 确认 → 异常终止
            handle.error = handle.error or "子进程未发送 committed 确认（异常退出）"
        self._sync_db_status(handle)
        if self.hub is not None:
            self.hub.publish_envelope(
                {
                    "type": "status",
                    "run_id": handle.run_id,
                    "ts": int(time_mod.time() * 1000),
                    "event_seq": 0,
                    "committed": True,
                    "data": {"status": handle.status, "terminal": True},
                }
            )

    # ------------------------------------------------------------------
    # DB（预建 running 行 + 终态兜底）
    # ------------------------------------------------------------------
    def _precreate_run(self, task: TaskConfig, run_id: str) -> None:
        try:
            db = init_db(self._db_url)
            repo = RunRepo(db)
            snap, _ = repo.get_or_create_snapshot(
                file_name=task.strategy.file,
                code_text=Path(task.strategy.file).read_text(encoding="utf-8"),
                sha256="",  # worker persist 时以 manifest 重算覆写（serve 预建占位）
            )
            repo.create_run(
                run_id=run_id,
                task_name=task.task_name,
                platform=task.strategy.type,
                snapshot_id=snap.id,
                params_json=json.dumps(
                    sanitize_params(json.loads(task.model_dump_json())),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                status=RUN_RUNNING,
                mtzquant_version="",
            )
        except Exception:  # noqa: BLE001 - 预建失败不阻断（worker persist 会建）
            pass

    def _sync_db_status(self, handle: RunHandle) -> None:
        try:
            db = init_db(self._db_url)
            RunRepo(db).update_status(handle.run_id, handle.status, error_log=handle.error or None)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """退出钩子: 终止全部子进程（进程树, 2.4）。"""
        from mtzquant.worker.isolate import _terminate_tree

        with self._lock:
            handles = list(self._handles.values())
        for h in handles:
            try:
                if h.proc is not None and h.proc.poll() is None:
                    _terminate_tree(h.proc)
            except Exception:  # noqa: BLE001
                pass
