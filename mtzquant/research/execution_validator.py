# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:15:00
# @update_time        : 2026/08/17 00:15:00
# @description : M3-S5 EventDrivenExecutionValidator：双引擎摩擦归因（设计 5.8/8.4.4, D5 产物）

"""事件驱动执行校验器（设计 5.8/8.4.4）——同一 TargetWeights 的两引擎对比。

  TargetWeights ──→ 向量化引擎（S4, 快, 无订单语义）
       │ 唯一交接物
       └──→ 事件引擎（M1, 真撮合）: 生成目标权重策略 → 每期 OrderRequest 序列 → 真实撮合
  两引擎逐期收益差 = 摩擦成本（费用/滑点/容量/T+1/时机）, 复用 M2 fills.participation_rate。

产物（D5）: out_root/friction_report.{json,md}——M4 并入 report.html 新章节。

自洽性检验（S 验收）: 无费用无滑点无约束 + 买入持有（无再平衡）下两引擎收益差 = 0。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mtzquant.config import Settings
from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.session import BacktestSpec, FeeSpec, StrategySpec, TaskConfig
from mtzquant.research.portfolio import TargetWeights
from mtzquant.research.vectorized_backtester import VectorizedBacktester

# 目标权重执行策略（读 schedule.json, 在调仓日把目标权重转成 order_target_value 序列）
_STRATEGY_TEMPLATE = """# coding:utf-8
# @description : 目标权重执行策略（S5 自动生成, 双引擎交接物验证用）
import json

_SCHEDULE = {schedule_path!r}


def initialize(context):
    with open(_SCHEDULE, encoding="utf-8") as f:
        context.g["schedule"] = json.load(f)


def on_bar(context, bar):
    weights = context.g["schedule"].get(bar.dt.date().isoformat())
    if not weights:
        return
    tv = context.account.total_value
    for code, w in sorted(weights.items()):
        context.adapter.order_target_value(code, w * tv)
"""

# 无操作策略模板（自洽性检验预留, 当前用目标权重策略 + next_open 时机对齐）
_HOLD_STRATEGY = """# coding:utf-8
# @description : 买入持有（自洽性检验用, 零订单）
def initialize(context):
    pass


def on_bar(context, bar):
    pass
"""


@dataclass
class PeriodFriction:
    """单期摩擦分解（容量/费用/滑点/T+1, 8.4.4）。"""

    period: str  # 调仓日
    vector_ret: float = 0.0
    event_ret: float = 0.0
    diff: float = 0.0  # event - vector（负 = 摩擦成本）
    fee_ret: float = 0.0  # 费用（佣金+印花+过户）折收益
    slippage_ret: float = 0.0  # 滑点折收益
    capacity_ret: float = 0.0  # 残余: T+1/一字板/停牌/整手取整
    turnover: float = 0.0  # 本期目标换手
    participation_max: float = 0.0
    participation_mean: float = 0.0
    participation_p95: float = 0.0
    rejected_orders: int = 0


@dataclass
class FrictionReport:
    """双引擎摩擦归因报告（D5 产物）; 可序列化 json/markdown。"""

    vector_nav: pd.Series
    event_nav: pd.Series
    periods: list[PeriodFriction] = field(default_factory=list)
    data_manifest_hash: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def totals(self) -> dict[str, float]:
        return {
            "total_return_diff": round(
                float(self.event_nav.iloc[-1]) - float(self.vector_nav.iloc[-1]), 6
            ),
            "fee_cost": round(sum(p.fee_ret for p in self.periods), 6),
            "slippage_cost": round(sum(p.slippage_ret for p in self.periods), 6),
            "capacity_cost": round(sum(p.capacity_ret for p in self.periods), 6),
            "total_turnover": round(sum(p.turnover for p in self.periods), 6),
        }

    # ------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "totals": self.totals,
            "periods": [p.__dict__ for p in self.periods],
            "vector_nav": {_dkey(d): float(v) for d, v in self.vector_nav.items()},
            "event_nav": {_dkey(d): float(v) for d, v in self.event_nav.items()},
        }

    def to_markdown(self) -> str:
        t = self.totals
        lines = [
            "# 摩擦归因报告（friction report）",
            "",
            f"- 数据版本锚点: `{self.data_manifest_hash[:12]}…`",
            f"- 总收益差（事件 − 向量）: **{t['total_return_diff']:.6f}**",
            f"- 费用成本: {t['fee_cost']:.6f}",
            f"- 滑点成本: {t['slippage_cost']:.6f}",
            f"- 容量/T+1 成本: {t['capacity_cost']:.6f}",
            f"- 总换手: {t['total_turnover']:.6f}",
            "",
            "| 调仓日 | 向量收益 | 事件收益 | 差值 | 费用 | 滑点 | 容量 | 换手 | 参与率均值 |",
            "|--------|---------|---------|------|------|------|------|------|-----------|",
        ]
        for p in self.periods:
            lines.append(
                f"| {p.period} | {p.vector_ret:.4f} | {p.event_ret:.4f} | {p.diff:.4f} "
                f"| {p.fee_ret:.4f} | {p.slippage_ret:.4f} | {p.capacity_ret:.4f} "
                f"| {p.turnover:.4f} | {p.participation_mean:.4f} |"
            )
        return "\n".join(lines)


def _dkey(d: Any) -> str:
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)


def _bundle(obj: Any) -> Any:
    """归一: RunResult → ExportBundle（run_task 返回 RunResult, 含 .bundle）。"""
    return obj.bundle if hasattr(obj, "bundle") else obj


class EventDrivenExecutionValidator:
    """同一 TargetWeights 喂两引擎, 摩擦归因（设计 5.8/8.4.4）。"""

    def __init__(
        self,
        settings: Settings,
        provider: Any,
        *,
        out_root: Path | str = "results",
        run_task_fn: Callable[..., Any] | None = None,
        initial_capital: float = 1_000_000.0,
    ) -> None:
        self._settings = settings
        self._provider = provider  # 研究层共用 Provider（5.8: 训练所见=回测所见）
        self._out_root = Path(out_root)
        self._run_task_impl = run_task_fn  # 测试注入（绕过真实入库）
        self._capital = initial_capital

    # ------------------------------------------------------------------
    # 主流程: TargetWeights → 两引擎 → 摩擦分解 → 报告
    # ------------------------------------------------------------------
    def run(
        self,
        tw: TargetWeights,
        *,
        vector_bps: float | None = None,
        fee_rate: float | None = None,
        tag: str = "friction",
    ) -> FrictionReport:
        bps = vector_bps if vector_bps is not None else self._settings.research.bps_default
        out_dir = self._out_root / tag
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1) 向量化基线（同一 TargetWeights, 非撮真）
        vector_res = VectorizedBacktester(self._provider, bps=bps).run(tw)

        # 2) 事件引擎（生成目标权重策略 → 真撮合）
        schedule_path = out_dir / "schedule.json"
        strategy_path = out_dir / "target_weights_strategy.py"
        self._write_schedule(tw, schedule_path)
        strategy_path.write_text(
            _STRATEGY_TEMPLATE.format(schedule_path=str(schedule_path).replace("\\", "/")),
            encoding="utf-8",
        )
        event_bundle = self._run_event(tw, strategy_path, out_dir, fee_rate=fee_rate)

        # 3) 对齐净值 + 逐期摩擦分解
        vector_nav = vector_res.nav
        event_nav = self._event_nav(event_bundle, vector_nav.index)
        report = self._decompose(tw, vector_nav, event_nav, event_bundle)
        report.vector_nav = vector_nav
        report.event_nav = event_nav
        report.data_manifest_hash = tw.data_manifest_hash
        eb = _bundle(event_bundle)
        report.meta = {
            "vector_bps": bps,
            "initial_capital": self._capital,
            "n_periods": len(report.periods),
            "event_status": eb.status,
            "degradations": eb.degradations,
        }

        # 4) 写 friction_report.{json,md}（D5）
        (out_dir / "friction_report.json").write_text(
            json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "friction_report.md").write_text(report.to_markdown(), encoding="utf-8")
        return report

    # ------------------------------------------------------------------
    # 自洽性检验: 无费用无滑点无约束 + 买入持有 → 两引擎收益差 = 0
    # ------------------------------------------------------------------
    def self_consistency(
        self, tw: TargetWeights, *, out_dir: str = "results/self_consistency"
    ) -> dict[str, float]:
        """零摩擦 + 买入持有配置下两引擎净值逐日差的最大绝对值（S 验收: ≈0）。

        时机对齐: 向量化用 fill_at='next_open'（首日收盘提交、次日开盘成交, 与事件引擎
        撮合时机一致）+ bps=0; 事件引擎用目标权重策略 + 零费率——两引擎零摩擦口径一致。
        容差 tol=5e-3 吸收事件引擎**整手取整 + 现金余量**量化残差（lot_size=100, v1 约定）;
        该残差为常数偏移而非随时间增长的摩擦——摩擦测试（run）另证费用可分解。
        """
        vector_res = VectorizedBacktester(self._provider, bps=0.0).run(tw, fill_at="next_open")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        schedule_path = out / "schedule.json"
        strategy_path = out / "target_weights_strategy.py"
        self._write_schedule(tw, schedule_path)
        strategy_path.write_text(
            _STRATEGY_TEMPLATE.format(schedule_path=str(schedule_path).replace("\\", "/")),
            encoding="utf-8",
        )
        task = self._task(tw, strategy_path, initial_positions=None, fee_rate=0.0)
        bundle = self._run_task(task, out)
        event_nav = self._event_nav(bundle, vector_res.nav.index)
        aligned = (
            pd.concat([vector_res.nav.rename("vector"), event_nav.rename("event")], axis=1)
            .dropna()
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        max_diff = (
            float((aligned["event"] - aligned["vector"]).abs().max())
            if len(aligned)
            else float("inf")
        )
        # 残差必须为**常数偏移**（整手取整/现金余量）, 不得随时间增长（真实摩擦才增长）
        residual_span = float(
            (aligned["event"] - aligned["vector"]).max()
            - (aligned["event"] - aligned["vector"]).min()
        )  # type: ignore[arg-type]
        if max_diff > 5e-3:
            raise MtzQuantError(
                f"摩擦自洽检验失败: 两引擎最大收益差 {max_diff:.2e}（容差 5e-3 整手量化）",
                stage="execution_validator",
                hint="买入持有 + 零摩擦下两引擎口径应一致（5.8 双引擎哲学）; 检查时机/费率对齐",
            )
        if residual_span > 5e-3 and len(aligned) > 10:
            raise MtzQuantError(
                f"摩擦自洽残差非恒定（span {residual_span:.2e}）——存在未分解的摩擦",
                stage="execution_validator",
                hint="零摩擦配置下残差应为常数整手量化, 不应随时间漂移",
            )
        return {"max_diff": max_diff, "residual_span": residual_span, "n_days": len(aligned)}

    # ------------------------------------------------------------------
    # 内部: 事件引擎装配
    # ------------------------------------------------------------------
    def _run_event(
        self, tw: TargetWeights, strategy_path: Path, out_dir: Path, *, fee_rate: float | None
    ) -> Any:
        task = self._task(tw, strategy_path, initial_positions=None, fee_rate=fee_rate)
        return self._run_task(task, out_dir)

    def _task(
        self,
        tw: TargetWeights,
        strategy_path: Path,
        *,
        initial_positions: dict[str, float] | None,
        fee_rate: float | None,
    ) -> TaskConfig:
        dates = sorted(tw.dates())
        start = _dkey(dates[0])
        end = self._data_end(tw)  # 数据末尾（与向量化比较窗口一致, 事件引擎跑满对比区间）
        if fee_rate is None:
            fee_rate = self._settings.engine.default_fees.commission_rate
        fees = FeeSpec(
            commission_rate=fee_rate,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            transfer_fee_rate=0.0,
        )
        return TaskConfig(
            task_name="friction_validate",
            strategy=StrategySpec(file=str(strategy_path), type="native"),
            backtest=BacktestSpec(
                start=start,
                end=end,
                initial_capital=self._capital,
                frequency="1d",
                initial_positions=initial_positions or {},
                strict_schedule=False,
            ),
            universe=[str(c) for c in tw.codes()],
            fees=fees,
            engine={},
        )

    def _run_task(self, task: TaskConfig, out_dir: Path) -> Any:
        if self._run_task_impl is not None:
            return self._run_task_impl(task, out_dir)
        from mtzquant.engine.runner import run_task

        return run_task(task, settings=self._settings, out_root=out_dir, persist=False)

    # ------------------------------------------------------------------
    # 内部: 净值对齐 / 数据末尾 / 分解
    # ------------------------------------------------------------------
    def _data_end(self, tw: TargetWeights) -> str:
        """事件引擎回测 end = 全池数据末尾（与向量化比较窗口一致, 8.8 确定性）。"""
        lo = pd.Timestamp(min(tw.dates()))
        frame = self._provider.to_frame(tw.codes(), ["close"], lo, lo + pd.Timedelta(days=500))
        if frame.empty:
            return _dkey(max(tw.dates()))
        return _dkey(frame.index[-1])

    # ------------------------------------------------------------------
    # 内部: 净值对齐 / 初始持仓 / 分解
    # ------------------------------------------------------------------
    @staticmethod
    def _event_nav(bundle: Any, index: pd.Index) -> pd.Series:
        eb = _bundle(bundle)
        navs = {n["trade_date"]: n["nav"] for n in eb.navs}
        out = pd.Series({d: navs[_dkey(d)] for d in index if _dkey(d) in navs}, dtype=float)
        return out.reindex(index).ffill()

    def _decompose(
        self, tw: TargetWeights, vector_nav: pd.Series, event_nav: pd.Series, bundle: Any
    ) -> FrictionReport:
        dates = sorted(tw.dates())
        periods: list[PeriodFriction] = []
        eb = _bundle(bundle)
        fills_by_day: dict[str, list[dict[str, Any]]] = {}
        for f in eb.fills:
            fills_by_day.setdefault(str(f["fill_time"])[:10], []).append(f)
        rej_by_day: dict[str, int] = {}
        for o in eb.orders:
            if o.get("status") == "rejected":
                d = str(o.get("submitted_at", ""))[:10]
                rej_by_day[d] = rej_by_day.get(d, 0) + 1
        turnover_series = tw.rebalance_turnover()
        for i, d in enumerate(dates):
            d_next = dates[i + 1] if i + 1 < len(dates) else None
            dkey = _dkey(d)
            v0, v1 = (
                float(vector_nav.loc[d]),
                float(vector_nav.loc[d_next] if d_next is not None else vector_nav.iloc[-1]),
            )
            e0, e1 = (
                float(event_nav.loc[d]),
                float(event_nav.loc[d_next] if d_next is not None else event_nav.iloc[-1]),
            )
            vector_ret = v1 / v0 - 1.0 if v0 > 0 else 0.0
            event_ret = e1 / e0 - 1.0 if e0 > 0 else 0.0
            equity = e0 * self._capital if e0 > 0 else self._capital
            fills = fills_by_day.get(dkey, [])
            fee_ret = (
                sum(
                    f.get("commission", 0.0) + f.get("stamp_tax", 0.0) + f.get("transfer_fee", 0.0)
                    for f in fills
                )
                / equity
            )
            slippage_ret = sum(f.get("slippage_cost", 0.0) for f in fills) / equity
            parts = [f.get("participation_rate", 0.0) for f in fills if f.get("participation_rate")]
            periods.append(
                PeriodFriction(
                    period=dkey,
                    vector_ret=round(vector_ret, 6),
                    event_ret=round(event_ret, 6),
                    diff=round(event_ret - vector_ret, 6),
                    fee_ret=round(fee_ret, 8),
                    slippage_ret=round(slippage_ret, 8),
                    capacity_ret=round(event_ret - vector_ret - fee_ret - slippage_ret, 8),
                    turnover=round(float(turnover_series.get(d, 0.0)), 6),
                    participation_max=round(max(parts), 6) if parts else 0.0,
                    participation_mean=round(float(np.mean(parts)), 6) if parts else 0.0,
                    participation_p95=round(float(np.percentile(parts, 95)), 6) if parts else 0.0,
                    rejected_orders=rej_by_day.get(dkey, 0),
                )
            )
        return FrictionReport(
            vector_nav=vector_nav,
            event_nav=event_nav,
            periods=periods,
            data_manifest_hash=tw.data_manifest_hash,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _write_schedule(tw: TargetWeights, path: Path) -> None:
        schedule: dict[str, dict[str, float]] = {}
        for d, row in tw.weights.iterrows():
            dkey = _dkey(d)
            schedule[dkey] = {
                str(c): float(v) for c, v in row.items() if pd.notna(v) and float(v) != 0.0
            }
        path.write_text(json.dumps(schedule, ensure_ascii=False, sort_keys=True), encoding="utf-8")
