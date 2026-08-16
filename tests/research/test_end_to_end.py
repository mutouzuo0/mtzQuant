# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 01:40:00
# @update_time        : 2026/08/17 01:40:00
# @description : M3-V 端到端 T-W01：因子→组合→TargetWeights→事件引擎→friction_report→report.html

"""T-W01（设计 12.1-M3 验收, V）——目标权重交接协议全链 + 摩擦归因并入报告。"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from mtzquant.engine.report import render_report
from mtzquant.optimize.orchestrate import run_scan
from mtzquant.optimize.queue import BacktestQueue
from mtzquant.research.execution_validator import EventDrivenExecutionValidator
from mtzquant.research.factors import FactorEngine
from mtzquant.research.portfolio import Constraints, PortfolioConstructor, TargetWeights

_SH = ZoneInfo("Asia/Shanghai")


def _at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 15, 0, tzinfo=_SH)


class TestEndToEnd:
    def test_full_chain_factor_to_friction_report(self, make_env) -> None:  # type: ignore[no-untyped-def]
        """目标权重交接协议跑通（12.1-M3 验收 1）: 因子→组合→TargetWeights→事件引擎→报告。"""
        # volatility=0 → 次日开盘=前收, 无跳空（避免事件引擎并发目标单现金超支, 纯链路验证）
        provider, settings, tmp = make_env(
            codes=("510300.SH", "510500.SH"), n=100, drift=0.0002, volatility=0.0
        )
        codes = ["510300.SH", "510500.SH"]
        # 1) 因子（S1）→ 打分（S3）
        scores = FactorEngine(provider).compute(
            "momentum_20", codes, _at(2020, 3, 1), _at(2020, 5, 31)
        )
        assert not scores.empty
        # 2) 组合构造 → TargetWeights（唯一交接物）
        tw = PortfolioConstructor(provider).build(
            scores,
            method="score_topn",
            constraints=Constraints(top_n=2, max_weight=0.8),
            data_manifest_hash="e2e_chain",
        )
        assert isinstance(tw, TargetWeights)
        assert tw.data_manifest_hash == "e2e_chain"
        # 3) 事件引擎精算 → 摩擦归因报告（S5, D5）
        val = EventDrivenExecutionValidator(
            settings, provider, out_root=tmp / "e2e", initial_capital=1_000_000.0
        )
        report = val.run(tw, vector_bps=10.0, fee_rate=0.0001)
        assert len(report.periods) >= 1
        friction_json = tmp / "e2e" / "friction" / "friction_report.json"
        assert friction_json.is_file()
        data = json.loads(friction_json.read_text(encoding="utf-8"))
        assert "totals" in data and "periods" in data
        # 4) report.html 并入摩擦归因章节（V-3）
        assert report.meta["event_status"] in ("completed_exact", "completed_degraded")
        # 事件 run 的 results 目录 = out_root/friction/<run_id>（run 的 out_dir=tag 目录）
        run_id = report.meta.get("run_id") or ""
        if run_id:
            html_path = render_report(
                run_id, out_root=tmp / "e2e" / "friction", friction_path=friction_json
            )
            html_text = html_path.read_text(encoding="utf-8")
            assert "摩擦归因报告" in html_text
            assert "总收益差" in html_text

    def test_grid_100_rank_all_event_top5(self, make_env) -> None:  # type: ignore[no-untyped-def]
        """普查/精算分工（12.1-M3 验收 2）: grid 100 组全量向量化排序, 事件引擎只精算 Top5。"""
        provider, settings, tmp = make_env(codes=("510300.SH",), n=60, drift=0.0005)
        submitted: list[dict] = []

        def executor(task: dict) -> dict:
            submitted.append(task)
            fast = task["engine"]["params"]["fast"]
            return {"run_id": f"r_{fast}", "sharpe": fast / 100.0}

        # 100 组直积空间（10 × 10 = 100）
        space = {"fast": list(range(1, 11)), "slow": list(range(20, 30))}
        queue = BacktestQueue(workers=2, state_dir=tmp / ".q", executor_fn=executor)

        def rank_fn(task: dict) -> float:
            # 向量化普查指标: 参数越大越高（示意; 真实场景用 VectorizedBacktester）
            return float(task["engine"]["params"]["fast"])

        res = run_scan(
            {"task_name": "grid100", "engine": {}},
            space,
            mode="grid",
            top=5,
            queue=queue,
            vector_rank_fn=rank_fn,
            metric_of=lambda rid: float(rid[2:]) / 100.0,
        )
        assert res.total == 100  # 全量展开
        assert len(submitted) == 5  # 事件引擎只精算 Top-5（普查 100 → 精算 5）
        assert len(res.run_ids) == 5
        # Top-1 是 fast=10（rank 最大）
        assert res.top[0]["params"]["fast"] == 10
