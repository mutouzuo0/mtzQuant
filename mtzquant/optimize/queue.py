# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:45:00
# @update_time        : 2026/08/17 00:45:00
# @description : M3-T2 BacktestQueue：subprocess worker 池 + 串行汇聚 + 中断恢复（设计 10.4/12.2）

"""批量回测队列（设计 10.4/12.2, D2）——扫描任务并行执行与汇聚。

- `submit_many(tasks, workers)`: 任务落 `state_dir/tasks/{id}.json` → worker 池并行执行;
  默认 executor = subprocess `mtzquant run -c task.json --json`（worker/ 隔离: 环境清洁
  + 超时进程树, 2.4）; 测试可注入进程内 executor。
- 串行汇聚（D2）: 父进程单点按完成序聚合结果（run_id/status/sharpe）到 queue_state.json,
  `on_each_done` 回调; SQLite WAL 单写连接由各 subprocess 承担（12.2 并发对策）。
- 中断恢复: 队列状态文件记录每任务状态; `resume()` 续跑 pending/failed, 已完成不重下。
- `selection_count`: 扫描总任务数写入各 run manifest（5.8.2, 由 optimize CLI 注入）。

确定性: 任务 id 由 params 规范化哈希生成（sort_keys, 8.8）; worker 数来自 settings.optimizer。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.manifest import canonical_json, sha256_text


@dataclass
class QueueTask:
    """单任务（含执行状态; 持久化到 queue_state.json）。"""

    task_id: str
    task: dict[str, Any]
    status: str = "pending"  # pending | running | done | failed
    run_id: str = ""
    sharpe: float | None = None
    error: str = ""


@dataclass
class QueueState:
    """队列持久化状态（中断恢复, 10.4）。"""

    version: int = 1
    tasks: dict[str, QueueTask] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tasks": {
                tid: {
                    "task_id": t.task_id,
                    "status": t.status,
                    "run_id": t.run_id,
                    "sharpe": t.sharpe,
                    "error": t.error,
                }
                for tid, t in self.tasks.items()
            },
        }


def _task_id(task: dict[str, Any]) -> str:
    """任务规范化哈希 id（同 params 同 id, 8.8 确定性）。"""
    return sha256_text(canonical_json(task))[:12]


class BacktestQueue:
    """批量回测队列（10.4/12.2, D2 串行汇聚）。"""

    def __init__(
        self,
        *,
        workers: int = 4,
        state_dir: Path | str = ".cache/queue",
        executor_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> None:
        self._workers = max(1, workers)
        self._state_dir = Path(state_dir)
        self._executor_fn = executor_fn  # 测试注入（进程内 run_task）
        self._timeout = timeout
        self._done_cbs: list[Callable[[str], None]] = []
        self._lock = threading.Lock()
        self._state = QueueState()
        self._load_state()

    # ------------------------------------------------------------------
    def on_each_done(self, cb: Callable[[str], None]) -> None:
        """注册每任务完成回调（run_id 参数; Top-N 实时刷新用, M4 扫描页）。"""
        self._done_cbs.append(cb)

    # ------------------------------------------------------------------
    def submit_many(self, tasks: list[dict[str, Any]], workers: int | None = None) -> list[str]:
        """提交批量任务并执行; 返回 run_ids（按完成序）。"""
        if not tasks:
            raise MtzQuantError("tasks 为空", stage="queue", hint="先构造扫描任务流（T1）")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        ids: list[str] = []
        for task in tasks:
            tid = _task_id(task)
            self._state.tasks.setdefault(tid, QueueTask(task_id=tid, task=task))
            self._state.tasks[tid].task = task  # 保留最新 task 定义
            if self._state.tasks[tid].status != "done":
                self._state.tasks[tid].status = "pending"
            ids.append(tid)
        self._save_state()
        n = workers or self._workers
        run_ids: list[str] = []
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [
                pool.submit(self._run_one, tid)
                for tid in ids
                if self._state.tasks[tid].status == "pending"
            ]
            for fut in futures:
                run_id = fut.result()
                run_ids.append(run_id)
        return run_ids

    def resume(self, workers: int | None = None) -> list[str]:
        """中断恢复: 续跑 pending/failed 任务（已完成不重下, 10.4）。"""
        pending = [tid for tid, t in self._state.tasks.items() if t.status != "done"]
        if not pending:
            return []
        return self.submit_many([self._state.tasks[tid].task for tid in pending], workers=workers)

    # ------------------------------------------------------------------
    def _run_one(self, tid: str) -> str:
        task = self._state.tasks[tid]
        with self._lock:
            task.status = "running"
            self._save_state()
        try:
            summary = self._execute(task.task)
            run_id = str(summary.get("run_id", ""))
            sharpe = _extract_sharpe(summary)
            with self._lock:
                task.status = "done"
                task.run_id = run_id
                task.sharpe = sharpe
                task.error = ""
                self._save_state()
            for cb in list(self._done_cbs):
                cb(run_id)
            return run_id
        except Exception as exc:  # noqa: BLE001 - 单任务失败不影响整批
            with self._lock:
                task.status = "failed"
                task.error = f"{type(exc).__name__}: {exc}"
                self._save_state()
            raise

    def _execute(self, task: dict[str, Any]) -> dict[str, Any]:
        """执行单任务; 默认 subprocess（隔离）, 可注入进程内执行器。"""
        if self._executor_fn is not None:
            return self._executor_fn(task)
        task_path = self._write_task_file(task)
        from mtzquant.worker.isolate import isolate_python_command, run_isolated

        wr = run_isolated(
            isolate_python_command(["run", "-c", str(task_path), "--json"]),
            timeout_seconds=self._timeout if self._timeout is not None else 3600,
        )
        if wr.timed_out or wr.returncode != 0:
            raise MtzQuantError(
                f"任务执行失败（rc={wr.returncode}）: {wr.stderr[:400] or wr.stdout[:400]}",
                stage="queue",
                hint="检查任务配置/数据覆盖（3.12）",
            )
        try:
            return json.loads(wr.stdout)
        except json.JSONDecodeError as exc:
            raise MtzQuantError(
                "队列子进程输出非 JSON",
                stage="queue",
                hint=f"stdout 前 200 字: {wr.stdout[:200]}",
            ) from exc

    def _write_task_file(self, task: dict[str, Any]) -> Path:
        tid = _task_id(task)
        p = self._state_dir / "tasks" / f"{tid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    # ------------------------------------------------------------------
    # 状态持久化（中断恢复）
    # ------------------------------------------------------------------
    def _state_path(self) -> Path:
        return self._state_dir / "queue_state.json"

    def _save_state(self) -> None:
        self._state_path().write_text(
            json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_state(self) -> None:
        p = self._state_path()
        if not p.is_file():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            for tid, t in raw.get("tasks", {}).items():
                self._state.tasks[tid] = QueueTask(task_id=tid, task={}, **t)
        except (OSError, ValueError, TypeError):
            return

    # ------------------------------------------------------------------
    @property
    def state(self) -> QueueState:
        return self._state

    def done_count(self) -> int:
        return sum(1 for t in self._state.tasks.values() if t.status == "done")

    def results(self) -> list[dict[str, Any]]:
        """已完成任务摘要（Top-N 排序源, 10.3）。"""
        out = []
        for t in sorted(self._state.tasks.values(), key=lambda x: x.task_id):
            if t.status == "done":
                out.append(
                    {
                        "task_id": t.task_id,
                        "params": (t.task.get("engine") or {}).get("params", {}),
                        "run_id": t.run_id,
                        "sharpe": t.sharpe,
                        "status": t.status,
                    }
                )
        return out


def _extract_sharpe(summary: dict[str, Any]) -> float | None:
    """从 run --json 摘要提取 sharpe（缺失 → None）。"""
    run = summary.get("run")
    if isinstance(run, dict):
        return run.get("sharpe")
    return None
