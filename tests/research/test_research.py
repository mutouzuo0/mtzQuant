# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:25:00
# @update_time        : 2026/08/17 00:25:00
# @description : M3-S 测试 T-V01..V06：因子/股票池/组合/向量化/摩擦归因/合成

"""T-V01..V06（设计 5.8/5.8.1, M3-S 向量化研究引擎）。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.research.alpha import AlphaCombiner, CapitalAllocator, StrategySleeve
from mtzquant.research.execution_validator import EventDrivenExecutionValidator
from mtzquant.research.factors import FactorEngine, list_factors
from mtzquant.research.optimizer import EqualWeightOptimizer
from mtzquant.research.portfolio import (
    Constraints,
    PortfolioConstructor,
    TargetWeights,
    zscore_cross_section,
)
from mtzquant.research.universe import (
    DelistedFilter,
    UniverseEngine,
)
from mtzquant.research.vectorized_backtester import VectorizedBacktester

_SH = ZoneInfo("Asia/Shanghai")


def _at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 15, 0, tzinfo=_SH)


# ============================================================
# T-V01: 因子值与手算向量对照
# ============================================================
class TestFactorEngine:
    def test_momentum_matches_manual(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _settings, tmp = make_env(codes=("510300.SH",), n=80)
        eng = FactorEngine(provider)
        df = eng.compute("momentum_20", ["510300.SH"], _at(2020, 3, 1), _at(2020, 4, 30))
        close = provider.to_frame(["510300.SH"], ["close"], _at(2020, 1, 1), _at(2020, 4, 30))
        manual = close["510300.SH"]["close"].pct_change(20)
        manual = manual.loc[manual.index >= pd.Timestamp(_at(2020, 3, 1))]
        common = df["510300.SH"].dropna().index.intersection(manual.dropna().index)
        np.testing.assert_allclose(
            df["510300.SH"].loc[common].to_numpy(),
            manual.loc[common].to_numpy(),
            rtol=1e-9,
        )

    def test_volatility_matches_manual(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _s, tmp = make_env(codes=("510300.SH",), n=80)
        eng = FactorEngine(provider)
        df = eng.compute("volatility_20", ["510300.SH"], _at(2020, 3, 1), _at(2020, 4, 30))
        close = provider.to_frame(["510300.SH"], ["close"], _at(2020, 1, 1), _at(2020, 4, 30))
        manual = close["510300.SH"]["close"].pct_change().rolling(20).std()
        manual = manual.loc[manual.index >= pd.Timestamp(_at(2020, 3, 1))]
        common = df["510300.SH"].dropna().index.intersection(manual.dropna().index)
        np.testing.assert_allclose(
            df["510300.SH"].loc[common].to_numpy(),
            manual.loc[common].to_numpy(),
            rtol=1e-9,
        )

    def test_factor_registry_lists_builtins(self) -> None:
        fs = list_factors()
        assert "momentum_20" in fs and "market_cap" in fs and "pe" in fs

    def test_unknown_factor_rejected(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _s, tmp = make_env(codes=("510300.SH",))
        with pytest.raises(MtzQuantError, match="未知因子"):
            FactorEngine(provider).compute("nope", ["510300.SH"], _at(2020, 3, 1), _at(2020, 4, 30))

    def test_fundamental_factor_asof(self, make_env) -> None:  # type: ignore[no-untyped-def]
        """市值因子: 只取 ≤end 已披露的 daily_basic（as-of, 前视偏差防护）。"""
        provider, _s, tmp = make_env(codes=("510300.SH",), n=60)
        # 写 daily_basic: 只有 2020-03 之前的值 → 之后全部前向填充
        rows = [
            {"ts_code": "510300.SH", "trade_date": "20200203", "total_mv": 1.0e6},
            {"ts_code": "510300.SH", "trade_date": "20200228", "total_mv": 2.0e6},
        ]
        p = tmp / "fundamentals" / "daily_basic" / "510300.SH.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8")
        df = FactorEngine(provider).compute(
            "market_cap", ["510300.SH"], _at(2020, 3, 1), _at(2020, 4, 30)
        )
        vals = df["510300.SH"].dropna()
        assert len(vals) > 0
        assert float(vals.iloc[0]) == 2.0e6  # 2020-02-28 值前向填充（as-of）


# ============================================================
# T-V02: 逐期股票池 + 退市过滤
# ============================================================
class TestUniverseEngine:
    def test_period_pool_and_delist_filter(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _s, tmp = make_env(codes=("510300.SH", "510500.SH", "600000.SH"), n=100)
        # 600000.SH 只在前 60 天有数据 → 之后视同退市/缺失
        arr = provider.bar_array("600000.SH")
        provider._arrays["600000.SH"] = arr[:60].copy()  # type: ignore[attr-defined]
        eng = UniverseEngine(provider, filters=[DelistedFilter()], rebalance="W")
        snaps = eng.run(date(2020, 1, 2), date(2020, 4, 30))
        assert len(snaps) > 0
        early = snaps[0]
        assert "510300.SH" in early.members
        assert "510500.SH" in early.members
        assert "600000.SH" in early.members
        # 末期 600000.SH 无 bar → 被退市/缺失过滤
        late = snaps[-1]
        if late.date >= date(2020, 3, 25):
            assert "600000.SH" not in late.members
            assert "600000.SH" in late.basis["rejected"]

    def test_monthly_rebalance_first_trading_day(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _s, tmp = make_env(codes=("510300.SH",), n=100)
        eng = UniverseEngine(provider, rebalance="M")
        dates = eng.rebalance_dates(date(2020, 1, 2), date(2020, 4, 30))
        months = {(d.year, d.month) for d in dates}
        assert len(months) == len(dates)  # 每月只一次


# ============================================================
# T-V03: 约束满足 / Z 分正确
# ============================================================
class TestPortfolioConstructor:
    def _scores(self, n_dates: int = 5, codes: list[str] | None = None) -> pd.DataFrame:
        codes = codes or ["A", "B", "C"]
        idx = pd.date_range("2020-01-01", periods=n_dates, freq="W")
        rng = np.random.default_rng(42)
        return pd.DataFrame(rng.normal(size=(n_dates, len(codes))), index=idx, columns=codes)

    def test_equal_weight_sums_to_one(self) -> None:
        scores = self._scores()
        tw = PortfolioConstructor().build(scores, method="equal", constraints=Constraints())
        assert np.allclose(tw.weights.sum(axis=1), 1.0)
        assert set(tw.codes()) == {"A", "B", "C"}

    def test_zscore_correct(self) -> None:
        df = pd.DataFrame({"A": [1.0, 2.0], "B": [3.0, 4.0], "C": [5.0, 6.0]})
        z = zscore_cross_section(df)
        # 每行 Z 分和=0, 标准差=1
        np.testing.assert_allclose(z.sum(axis=1).to_numpy(), np.zeros(2), atol=1e-9)
        np.testing.assert_allclose(z.std(axis=1).to_numpy(), np.ones(2), atol=1e-9)

    def test_max_weight_constraint(self) -> None:
        scores = pd.DataFrame(
            {"A": [1.0, 1.0], "B": [1.0, 1.0], "C": [1.0, 1.0]},
            index=pd.date_range("2020-01-01", periods=2, freq="W"),
        )
        tw = PortfolioConstructor().build(
            scores, method="score_topn", constraints=Constraints(max_weight=0.5, top_n=2)
        )
        assert float(tw.weights.max().max()) <= 0.5 + 1e-9

    def test_long_only_removes_negative(self) -> None:
        scores = pd.DataFrame(
            {"A": [1.0], "B": [-1.0], "C": [0.5]}, index=pd.date_range("2020-01-01", periods=1)
        )
        tw = PortfolioConstructor().build(
            scores, method="score_topn", constraints=Constraints(long_only=True)
        )
        row = tw.weights.iloc[0]
        assert float(row["B"]) == 0.0
        assert float(row.sum()) == pytest.approx(1.0)


# ============================================================
# T-V04: 权益计算对照（买入持有 vs 手算）
# ============================================================
class TestVectorizedBacktester:
    def test_buy_and_hold_matches_manual(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _s, tmp = make_env(codes=("510300.SH", "510500.SH"), n=60, drift=0.001)
        start = provider.to_frame(
            ["510300.SH"], ["close"], _at(2020, 1, 2), _at(2020, 4, 30)
        ).index[0]
        w = pd.DataFrame({"510300.SH": [0.6], "510500.SH": [0.4]}, index=pd.DatetimeIndex([start]))
        tw = TargetWeights(weights=w, data_manifest_hash="abc")
        res = VectorizedBacktester(provider, bps=0.0).run(tw)
        # 手算: nav_t = Σ w_i * close_t/close_0 + (1-Σw)
        closes = provider.to_frame(
            ["510300.SH", "510500.SH"], ["close"], _at(2020, 1, 2), _at(2020, 4, 30)
        )
        close0 = {c: float(closes[c]["close"].iloc[0]) for c in ("510300.SH", "510500.SH")}
        manual = []
        for _ts, row in closes.iterrows():
            nav = 0.6 * float(row["510300.SH"]["close"]) / close0["510300.SH"]
            nav += 0.4 * float(row["510500.SH"]["close"]) / close0["510500.SH"]
            manual.append(nav)
        np.testing.assert_allclose(
            res.nav.dropna().to_numpy(), np.asarray(manual, dtype=float), rtol=1e-9
        )

    def test_rebalance_turnover_and_cost(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _s, tmp = make_env(codes=("510300.SH",), n=40)
        start = provider.to_frame(["510300.SH"], ["close"], _at(2020, 1, 2), _at(2020, 3, 31)).index
        d1, d2 = start[0], start[20]
        w = pd.DataFrame({"510300.SH": [1.0, 0.0]}, index=pd.DatetimeIndex([d1, d2]))
        tw = TargetWeights(weights=w)
        res = VectorizedBacktester(provider, bps=10.0).run(tw)
        assert float(res.turnover.sum()) == pytest.approx(1.0, abs=1e-9)  # 全仓→空仓换手 1
        assert float(res.nav.iloc[-1]) <= float(res.gross_nav.iloc[-1])  # 费后 <= 费前

    def test_non_matching_manifest_rejected(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, _s, tmp = make_env(codes=("510300.SH",), n=40)
        start = provider.to_frame(
            ["510300.SH"], ["close"], _at(2020, 1, 2), _at(2020, 3, 31)
        ).index[0]
        w = pd.DataFrame({"510300.SH": [1.0]}, index=pd.DatetimeIndex([start]))
        tw = TargetWeights(weights=w, data_manifest_hash="hashA")
        bt = VectorizedBacktester(provider)
        with pytest.raises(MtzQuantError, match="数据版本"):
            bt.require_manifest(tw, "hashB")


# ============================================================
# T-V05: 摩擦归因数值 + 自洽性检验
# ============================================================
class TestExecutionValidator:
    def test_self_consistency_zero_friction(self, make_env) -> None:  # type: ignore[no-untyped-def]
        """无费用无滑点无约束 + 买入持有 → 两引擎收益差 ≈0（S 验收; 容差=整手量化残差）。"""
        provider, settings, tmp = make_env(codes=("510300.SH", "510500.SH"), n=80, drift=0.0005)
        start = provider.to_frame(
            ["510300.SH"], ["close"], _at(2020, 1, 2), _at(2020, 4, 30)
        ).index[0]
        w = pd.DataFrame({"510300.SH": [0.6], "510500.SH": [0.4]}, index=pd.DatetimeIndex([start]))
        tw = TargetWeights(weights=w, data_manifest_hash="consist")
        val = EventDrivenExecutionValidator(
            settings, provider, out_root=tmp / "out", run_task_fn=None, initial_capital=1_000_000.0
        )
        result = val.self_consistency(tw, out_dir=str(tmp / "sc"))
        # 残差上限 = 整手取整/现金余量（v1 lot_size=100, 常数偏移）
        assert result["max_diff"] < 5e-3
        assert result["residual_span"] < 5e-3  # 恒定偏移, 非增长摩擦

    def test_friction_report_decomposition(self, make_env) -> None:  # type: ignore[no-untyped-def]
        provider, settings, tmp = make_env(codes=("510300.SH", "510500.SH"), n=100, drift=0.001)
        closes = provider.to_frame(
            ["510300.SH", "510500.SH"], ["close"], _at(2020, 1, 2), _at(2020, 5, 31)
        )
        d1, d2, d3 = closes.index[10], closes.index[30], closes.index[60]
        w = pd.DataFrame(
            {
                "510300.SH": [0.7, 0.3, 0.5],
                "510500.SH": [0.3, 0.7, 0.5],
            },
            index=pd.DatetimeIndex([d1, d2, d3]),
        )
        tw = TargetWeights(weights=w, data_manifest_hash="friction1")
        val = EventDrivenExecutionValidator(
            settings, provider, out_root=tmp / "out", initial_capital=1_000_000.0
        )
        report = val.run(tw, vector_bps=10.0, fee_rate=0.0001)
        assert len(report.periods) == 3
        # 事件净值 <= 向量净值（摩擦非正）
        assert report.totals["total_return_diff"] <= 1e-9
        assert report.totals["total_turnover"] > 0
        # 报告产物落盘（D5）
        assert (tmp / "out" / "friction" / "friction_report.json").is_file()
        assert (tmp / "out" / "friction" / "friction_report.md").is_file()
        # markdown 可解析为表格
        md = report.to_markdown()
        assert "摩擦归因报告" in md and "| 调仓日 |" in md


# ============================================================
# T-V06: 组合抽象（alpha 合成 / 资本分配 / 优化器）
# ============================================================
class TestAlphaAndOptimizer:
    def test_alpha_combiner_weights(self) -> None:
        idx = pd.date_range("2020-01-01", periods=3)
        a1 = pd.DataFrame({"A": [1.0, 2.0, 3.0]}, index=idx)
        a2 = pd.DataFrame({"B": [0.5, 1.5, 2.5]}, index=idx)
        comb = AlphaCombiner(mode="equal")
        out = comb.combine({"s1": a1, "s2": a2})
        assert "A" in out.columns and "B" in out.columns
        assert abs(float(out.loc[idx[0], "A"]) - 0.5) < 1e-9

    def test_capital_allocator_fixed(self) -> None:
        alloc = CapitalAllocator(mode="fixed")
        out = alloc.allocate([StrategySleeve("a", weight=2.0), StrategySleeve("b", weight=1.0)])
        assert out["a"] == pytest.approx(2 / 3)
        assert out["b"] == pytest.approx(1 / 3)

    def test_equal_weight_optimizer(self) -> None:
        er = pd.DataFrame(
            {"A": [0.1], "B": [0.05], "C": [-0.02]},
            index=pd.date_range("2020-01-01", periods=1),
        )
        opt = EqualWeightOptimizer()
        w = opt.optimize(er, Constraints(max_weight=0.8))
        row = w.iloc[0]
        assert float(row["C"]) == 0.0  # 负期望不配
        assert float(row["A"]) == pytest.approx(0.5)  # 等权于 A/B
