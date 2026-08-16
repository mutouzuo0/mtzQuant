# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 01:00:00
# @update_time        : 2026/08/17 01:00:00
# @description : M3-T 测试 T-O01..O04：展开/确定性/并行汇聚谱系/邻域表

"""T-O01..O04（设计 10.4/5.8.2, M3-T 参数优化与批量队列）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtzquant.optimize.orchestrate import run_scan
from mtzquant.optimize.queue import BacktestQueue
from mtzquant.optimize.scanner import ParamOptimizer
from mtzquant.optimize.sensitivity import build_neighborhood, scan_sensitivity

BASE_TASK = {"task_name": "scan_base", "engine": {}}


# ============================================================
# T-O01: grid 展开正确 / random 确定性
# ============================================================
class TestParamOptimizer:
    def test_grid_expansion_product(self) -> None:
        tasks = list(ParamOptimizer().search(BASE_TASK, {"fast": [5, 10], "slow": [20, 40, 60]}))
        assert len(tasks) == 6  # 2 × 3 直积
        params = [t["engine"]["params"] for t in tasks]
        assert {"fast": 5, "slow": 20} in params
        assert {"fast": 10, "slow": 60} in params
        # base_task 不被污染（深拷贝, 8.8 确定性）
        assert BASE_TASK["engine"] == {}

    def test_random_deterministic_seed(self) -> None:
        p = ParamOptimizer(seed=42)
        t1 = list(
            p.search(BASE_TASK, {"fast": [5, 10, 20], "slow": [40, 60]}, mode="random", n_trials=6)
        )
        t2 = list(
            p.search(BASE_TASK, {"fast": [5, 10, 20], "slow": [40, 60]}, mode="random", n_trials=6)
        )
        assert t1 == t2  # 同 seed 两次扫描任务序列全同（确定性, 8.8）
        assert len(t1) == 6

    def test_unknown_mode_rejected(self) -> None:
        from mtzquant.core.errors import MtzQuantError

        with pytest.raises(MtzQuantError, match="bayesian"):
            list(ParamOptimizer().search(BASE_TASK, {"fast": [1]}, mode="bayesian"))


# ============================================================
# T-O02: 并行执行 / 汇聚一致 / 中断恢复
# ============================================================
class TestBacktestQueue:
    def test_parallel_aggregation(self, tmp_path: Path) -> None:
        def executor(task: dict) -> dict:
            fast = task["engine"]["params"]["fast"]
            return {"run_id": f"r_{fast}", "sharpe": fast / 100.0}

        queue = BacktestQueue(workers=3, state_dir=tmp_path / ".q", executor_fn=executor)
        done: list[str] = []
        queue.on_each_done(lambda rid: done.append(rid))
        tasks = [{"task_name": "t", "engine": {"params": {"fast": f}}} for f in (5, 10, 20)]
        ids = queue.submit_many(tasks)
        assert len(ids) == 3
        assert queue.done_count() == 3
        assert sorted(done) == sorted(ids)  # 每任务回调一次
        assert len(queue.results()) == 3  # 汇聚一致

    def test_resume_skips_done(self, tmp_path: Path) -> None:
        def executor(task: dict) -> dict:
            return {"run_id": f"r_{task['engine']['params']['fast']}", "sharpe": 0.5}

        queue = BacktestQueue(workers=2, state_dir=tmp_path / ".q", executor_fn=executor)
        tasks = [{"task_name": "t", "engine": {"params": {"fast": f}}} for f in (1, 2)]
        queue.submit_many(tasks)
        assert queue.resume() == []  # 全部 done → 无续跑（不重下, 10.4）

    def test_failure_does_not_block_batch(self, tmp_path: Path) -> None:
        def executor(task: dict) -> dict:
            if task["engine"]["params"]["fast"] == 10:
                raise RuntimeError("boom")
            return {"run_id": "r_ok", "sharpe": 0.5}

        queue = BacktestQueue(workers=2, state_dir=tmp_path / ".q", executor_fn=executor)
        tasks = [{"task_name": "t", "engine": {"params": {"fast": f}}} for f in (5, 10, 20)]
        try:
            queue.submit_many(tasks)
        except RuntimeError:
            pass
        # 失败任务标记 failed, 其余 done（单任务失败不影响整批）
        statuses = {t.status for t in queue.state.tasks.values()}
        assert "failed" in statuses
        assert "done" in statuses


# ============================================================
# T-O03: 谱系链完整 / Top-N 排序 / selection_count
# ============================================================
class TestScanOrchestration:
    def test_lineage_and_topn(self, tmp_path: Path) -> None:
        captured: list[dict] = []

        def executor(task: dict) -> dict:
            captured.append(task)
            fast = task["engine"]["params"]["fast"]
            return {"run_id": f"r_{fast}", "sharpe": fast / 100.0}

        queue = BacktestQueue(workers=2, state_dir=tmp_path / ".q", executor_fn=executor)
        res = run_scan(
            BASE_TASK,
            {"fast": [5, 10, 20]},
            mode="grid",
            parent="base_run",
            top=2,
            queue=queue,
            metric_of=lambda rid: float(rid[2:]) / 100.0,
        )
        assert res.total == 3
        # 谱系链: 每扫描任务 parent_run_id 指向基线 run（10.3）
        for t in captured:
            assert t["engine"]["parent_run_id"] == "base_run"
            # selection_count = 该参数共尝试组数（5.8.2）
            assert t["engine"]["selection_count"] == 3
        # Top-N 排序（sharpe 降序）
        assert res.top[0]["params"] == {"fast": 20}
        assert res.top[0]["run_id"] == "r_20"
        assert len(res.top) == 2

    def test_scan_injects_manifest_fields(self, tmp_path: Path) -> None:
        """扫描任务的 engine 字段可被 manifest 消费（selection_count/parent_lineage, 5.8.2）。"""
        from mtzquant.config import DataSettings, LocalCsvSettings, Settings
        from mtzquant.engine.manifest import build_manifest

        task = {
            "task_name": "x",
            "engine": {"selection_count": 8, "parent_lineage": {"split": "train"}},
        }
        settings = Settings(data=DataSettings(local_csv=LocalCsvSettings(root_path=str(tmp_path))))
        manifest, _ = build_manifest(
            task,
            "def initialize(c): pass\n",
            driver=_FakeDriver(tmp_path),
            universe=[],
            settings=settings,
        )  # type: ignore[attr-defined]
        assert manifest["selection_count"] == 8
        assert manifest["parent_lineage"] == {"split": "train"}


class _FakeDriver:
    """build_manifest 最小桩（只提供 kline_path/settings）。"""

    def __init__(self, root: Path) -> None:
        self.settings = __import__("types").SimpleNamespace(
            root_path=str(root),
            calendar_dir="calendars",
            master_dir="master",
            corporate_actions_dir="corporate_actions",
            encoding="utf-8",
        )

    def kline_path(self, code: str, frequency):  # type: ignore[no-untyped-def]
        return Path(self.settings.root_path) / "kline" / "x" / f"{code}.csv"


# ============================================================
# T-O04: 邻域表生成 / 尖锐峰值门禁
# ============================================================
class TestSensitivity:
    def test_neighborhood_table(self) -> None:
        params = {"fast": 10, "slow": 30}
        nbrs = build_neighborhood(params, step_ratio=0.1, min_step=1)
        assert {"fast": 9, "slow": 30} in nbrs
        assert {"fast": 11, "slow": 30} in nbrs
        assert {"fast": 10, "slow": 27} in nbrs
        assert {"fast": 10, "slow": 33} in nbrs
        # 整数参数步长 = max(1, round(0.1*param)) → fast ±1, slow ±3
        assert {"fast": 10, "slow": 30} not in nbrs  # 不含基线

    def test_flat_neighborhood_allowed(self) -> None:
        params = {"fast": 10, "slow": 30}

        def metric(p: dict) -> float:
            # 基线最优 → 非尖锐, 可 adopted
            return 1.0 - abs(p["fast"] - 10) - abs(p["slow"] - 30)

        rep = scan_sensitivity(params, metric, step_ratio=0.1)
        assert rep.base_metric == 1.0
        assert rep.sharp_peak is False
        assert rep.adopted_allowed() is True  # 门禁通过

    def test_sharp_peak_blocks_adopted(self) -> None:
        params = {"fast": 10, "slow": 30}

        def metric(p: dict) -> float:
            # 邻域显著更优（0.5→1.0）→ 尖锐峰值, 参数脆弱
            return 1.0 if p != params else 0.5

        rep = scan_sensitivity(params, metric, step_ratio=0.1, peak_threshold=0.25)
        assert rep.sharp_peak is True
        assert rep.peak_ratio > 0.25
        assert rep.adopted_allowed() is False  # 不出/尖锐邻域报告 → 不允许 adopted

    def test_report_serializable(self) -> None:
        params = {"fast": 10, "slow": 30}
        rep = scan_sensitivity(params, lambda p: 1.0 - abs(p["fast"] - 10), step_ratio=0.1)
        data = rep.to_json()
        assert "adopted_allowed" in data and "neighbors" in data
        assert "邻域敏感性" in rep.to_markdown()
