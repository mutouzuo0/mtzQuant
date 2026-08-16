# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:55:00
# @update_time        : 2026/08/17 00:55:00
# @description : M3-T3 扫描编排：scan → 批量队列 → Top-N 汇总（谱系/selection_count, 10.3/10.4）

"""参数扫描编排（M3-T3）——CLI `mtzquant optimize` 的核心。

流程: ParamOptimizer 展开任务流 → 注入 selection_count（该参数共尝试组数, 5.8.2）与
parent_run_id（谱系指向基线 run, 10.3）→ BacktestQueue 批量执行 → 指标排序 Top-N。

产出: ScanResult（total/run_ids/top）+ 可选 Top-1 邻域敏感性（T4）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mtzquant.core.errors import MtzQuantError
from mtzquant.optimize.queue import BacktestQueue
from mtzquant.optimize.scanner import ParamOptimizer
from mtzquant.optimize.sensitivity import SensitivityReport, scan_sensitivity


@dataclass
class ScanResult:
    """一次扫描的完整产出（Top-N 汇总, 10.4）。"""

    total: int
    run_ids: list[str] = field(default_factory=list)
    top: list[dict[str, Any]] = field(default_factory=list)  # [{params, run_id, sharpe}]
    sensitivity: SensitivityReport | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "run_ids": self.run_ids,
            "top": self.top,
            "sensitivity": self.sensitivity.to_json() if self.sensitivity else None,
        }


def run_scan(
    base_task: dict[str, Any],
    space: dict[str, list[Any]],
    *,
    mode: str = "grid",
    n_trials: int | None = None,
    parent: str | None = None,
    top: int = 5,
    queue: BacktestQueue | None = None,
    metric_of: Callable[[str], float | None] | None = None,
    run_sensitivity: bool = False,
    sensitivity_metric: Callable[[dict[str, Any]], float] | None = None,
) -> ScanResult:
    """执行一次扫描; 返回 Top-N 汇总（含谱系与 selection_count 注入）。"""
    tasks = list(ParamOptimizer().search(base_task, space, mode=mode, n_trials=n_trials))
    total = len(tasks)
    if total == 0:
        raise MtzQuantError("扫描任务流为空", stage="optimize", hint="检查 --space")
    # 注入 selection_count（5.8.2）与谱系（10.3）——每扫描任务 run 的 manifest/父链
    for task in tasks:
        engine = dict(task.get("engine") or {})
        engine["selection_count"] = total
        if parent:
            engine["parent_run_id"] = parent
        task["engine"] = engine
    q = queue or BacktestQueue()
    run_ids = q.submit_many(tasks)
    # 指标排序 → Top-N（metric_of 缺省从 DB metrics 读 sharpe）
    rows: list[dict[str, Any]] = []
    for tid in [t.task_id for t in q.state.tasks.values() if t.status == "done"]:
        t = q.state.tasks[tid]
        params = (t.task.get("engine") or {}).get("params", {})
        sharpe = metric_of(t.run_id) if metric_of else _db_sharpe(t.run_id)
        rows.append(
            {
                "params": params,
                "run_id": t.run_id,
                "sharpe": sharpe,
            }
        )
    rows.sort(key=lambda r: (r["sharpe"] is None, -(r["sharpe"] or 0.0)))
    result = ScanResult(total=total, run_ids=run_ids, top=rows[: max(top, 1)])
    # 邻域敏感性（T4）: 对 Top-1 参数扫描（向量化引擎跑, 成本可控——§8）
    if run_sensitivity and result.top and result.top[0]["params"]:
        if sensitivity_metric is None:
            raise MtzQuantError(
                "run_sensitivity 需要 sensitivity_metric 注入（邻域用向量化引擎跑）",
                stage="optimize",
                hint="将 params → 向量化净值/指标; M3-T4",
            )
        result.sensitivity = scan_sensitivity(result.top[0]["params"], sensitivity_metric)
    return result


def _db_sharpe(run_id: str) -> float | None:
    """从 DB metrics 读 sharpe（CLI 生产路径; 缺库/缺失 → None）。"""
    try:
        from mtzquant.config import load_settings
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        settings = load_settings()
        repo = RunRepo(init_db(settings.database.url))
        m = repo.get_metrics(run_id)
        if m is None:
            return None
        return m.get("metrics", {}).get("sharpe")
    except Exception:  # noqa: BLE001 - Top-N 缺指标按 None 排尾, 不阻断
        return None
