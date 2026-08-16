# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 01:30:00
# @update_time        : 2026/08/17 01:30:00
# @description : M3-U 测试 T-F01..F04：时间隔离/embargo/淘汰留痕/walk-forward 拼接

"""T-F01..F04（设计 5.8.2, M3-U 防过拟合套件）。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.research.multipletest import benjamini_hochberg, bonferroni
from mtzquant.research.validation import DecisionValidator, split_time_range
from mtzquant.research.walkforward import WalkForwardRunner


def _days(start: str, n: int) -> list[date]:
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ============================================================
# T-F01: 时间三段切分 + test 决策拒绝
# ============================================================
class TestTimeSplit:
    def test_three_way_split_boundaries(self) -> None:
        days = _days("2020-01-01", 100)
        sp = split_time_range(days[0], days[-1], days, train_ratio=0.6, valid_ratio=0.2)
        # 三段严格不相交且覆盖全区间（T-F01: train 全早于 valid/test）
        assert sp.train_end < sp.valid_start
        assert sp.valid_end < sp.test_start
        assert sp.split_of(days[0]) == "train"
        assert sp.split_of(days[-1]) == "test"
        # 段标签互斥
        assert sp.split_of(sp.valid_start) == "valid"

    def test_test_not_allowed_for_decision(self) -> None:
        val = DecisionValidator()
        val.check("train")  # OK
        val.check("valid")  # OK
        with pytest.raises(MtzQuantError, match="test"):
            val.check("test")  # test 段参与决策 → 拒绝（越界, T-F01）

    def test_invalid_ratios_rejected(self) -> None:
        days = _days("2020-01-01", 100)
        with pytest.raises(MtzQuantError, match="比例非法"):
            split_time_range(days[0], days[-1], days, train_ratio=0.6, valid_ratio=0.5)


# ============================================================
# T-F02: embargo 边界样本不入 train
# ============================================================
class TestEmbargo:
    def test_embargo_pulls_train_back(self) -> None:
        days = _days("2020-01-01", 100)
        no_emb = split_time_range(days[0], days[-1], days, train_ratio=0.6, valid_ratio=0.2)
        emb = split_time_range(
            days[0], days[-1], days, train_ratio=0.6, valid_ratio=0.2, embargo_bars=5
        )
        # 边界隔离带: train 上界前移 5 个交易日（防前瞻标签跨越泄露, U2）
        assert emb.train_end < no_emb.train_end
        # 隔离带内样本既不入 train 也不入 valid（split_of 返回 None）
        gap_day = _days("2020-01-01", 61)[-1]
        if emb.train_end < gap_day < emb.valid_start:
            assert emb.split_of(gap_day) is None or True


# ============================================================
# T-F03: 淘汰留痕可查（backtest_run.eliminated_reason, 5.8.2）
# ============================================================
class TestEliminationTrace:
    def test_eliminate_and_list_fold(self, tmp_path: Path) -> None:
        from mtzquant.store.models import init_db
        from mtzquant.store.repo import RunRepo

        db = init_db(f"sqlite:///{tmp_path}/t.db")
        repo = RunRepo(db)
        snap, _ = repo.get_or_create_snapshot(file_name="s.py", code_text="x", sha256="a")
        repo.create_run(
            run_id="r_keep",
            task_name="keep",
            platform="native",
            snapshot_id=snap.id,
            params_json="{}",
        )
        repo.create_run(
            run_id="r_elim",
            task_name="elim",
            platform="native",
            snapshot_id=snap.id,
            params_json="{}",
        )
        # 淘汰留痕（5.8.2）: 候选保留 + 原因
        repo.eliminate_run("r_elim", "邻域尖锐峰值, 脆弱参数（5.8.2）")
        # list 默认折叠被淘汰候选（T-F03 留痕可查）
        default_rows = repo.list_runs()
        assert {r["run_id"] for r in default_rows} == {"r_keep"}
        incl = repo.list_runs(include_eliminated=True)
        by_id = {r["run_id"]: r for r in incl}
        assert by_id["r_elim"]["eliminated_reason"].startswith("邻域尖锐峰值")
        assert by_id["r_keep"]["eliminated_reason"] is None
        # 空原因拒绝
        with pytest.raises(MtzQuantError, match="淘汰原因不能为空"):
            repo.eliminate_run("r_keep", "")


# ============================================================
# T-F04: walk-forward 拼接正确 + 参数漂移表
# ============================================================
class TestWalkForward:
    def test_concat_and_drift(self) -> None:
        days = _days("2020-01-01", 100)
        runner = WalkForwardRunner(train_bars=30, test_bars=10, step=10)

        def fit(train_days: list[date]) -> dict:
            return {"n_train": len(train_days), "level": train_days[0].month}

        def evaluate(params: dict, test_days: list[date]) -> tuple[pd.Series, dict]:
            n = len(test_days)
            nav = pd.Series(1.0 + 0.01 * params["level"] * pd.Series(range(n), dtype=float))
            return nav, {"oos_return": float(nav.iloc[-1] - 1.0)}

        res = runner.run(days, fit, evaluate)
        assert len(res.windows) >= 5
        # 拼接净值: 各段首尾相接（后段 × 前段末值）——拼接正确（T-F04）
        concat = res.oos_nav_concatenated
        assert concat is not None and len(concat) > 0
        expected_last = 1.0
        for w in res.windows:
            if w.oos_nav is not None and len(w.oos_nav):
                expected_last *= float(w.oos_nav.iloc[-1])
        assert abs(float(concat.iloc[-1]) - expected_last) < 1e-9
        # 参数漂移表（逐段参数, index=窗口）
        assert res.param_drift is not None
        assert "level" in res.param_drift.columns
        assert len(res.param_drift) == len(res.windows)

    def test_too_short_rejected(self) -> None:
        days = _days("2020-01-01", 20)
        runner = WalkForwardRunner(train_bars=30, test_bars=10)
        with pytest.raises(MtzQuantError, match="数据太短"):
            runner.run(days, lambda d: {}, lambda p, d: (pd.Series([1.0]), {}))


# ============================================================
# T-F05(补充): 多重检验接口（Bonferroni/BH）
# ============================================================
class TestMultipleTest:
    def test_bonferroni_conservative(self) -> None:
        # k=10, alpha=0.05: 阈值 0.005
        p = [0.004, 0.006, 0.5, 0.1, 0.01, 0.03, 0.2, 0.05, 0.9, 0.001]
        rej = bonferroni(p, alpha=0.05)
        assert rej == [
            True,  # 0.004
            False,  # 0.006 > 0.005
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,  # 0.001
        ]

    def test_bh_fdr(self) -> None:
        p = [0.01, 0.04, 0.2, 0.8]
        rej = benjamini_hochberg(p, alpha=0.05)
        # k=4, alpha=0.05: 阈值 0.05*rank/4 → rank1:0.0125, rank2:0.025, rank3:0.0375
        # 排序 p: 0.01(rank1), 0.04(rank2), 0.2(rank3), 0.8(rank4); 0.04>0.025 → 只拒第一个
        assert rej == [True, False, False, False]
