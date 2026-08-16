# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:05:00
# @update_time        : 2026/08/17 00:05:00
# @description : M3-S4 VectorizedBacktester：目标权重级快速回测（D6 边界：非撮真, 设计 5.8）

"""向量化回测器（设计 5.8, D6 语义边界）——**非撮真**。

- 逐期调仓: 只在调仓时点按目标权重在**收盘价**调整（D6: 无逐单订单语义）;
- 费用: 按 configurable bps 近似（默认与任务费率同源换算, settings.research.bps_default）;
- 输出: 净值 / 换手 / 逐期持仓权重; 与事件引擎共用 Provider 数据版本
  （TargetWeights.data_manifest_hash 一致性断言）。

⚠️ 本类只做目标权重级回测, **禁止"顺便"实现撮合/订单语义**——订单语义验证一律归
事件驱动引擎（M1, EventDrivenExecutionValidator 比对两引擎差异 = 摩擦成本, S5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from mtzquant.core.errors import MtzQuantError
from mtzquant.research.portfolio import TargetWeights


@dataclass
class VectorizedResult:
    """向量化回测产出（研究普查, 非撮真语义）。"""

    nav: pd.Series  # 净费净值（index=交易日, 起始=1.0）
    gross_nav: pd.Series  # 不计费用净值（自洽性对照, S 验收）
    weights_held: pd.DataFrame  # 每日实际权重（index=交易日, columns=codes）
    turnover: pd.Series  # 每期换手（index=调仓时点, Σ|Δw|/2）
    data_manifest_hash: str = ""

    def summary(self) -> dict[str, float]:
        total = float(self.nav.iloc[-1]) - 1.0
        gross = float(self.gross_nav.iloc[-1]) - 1.0
        return {
            "total_return": round(total, 6),
            "gross_return": round(gross, 6),
            "friction_cost": round(gross - total, 6),
            "total_turnover": round(float(self.turnover.sum()), 6),
            "n_days": len(self.nav),
        }


class VectorizedBacktester:
    """目标权重级快速回测（设计 5.8, D6 边界: 非撮真）。

    fill_at: 'close'（默认, D6: 调仓时点收盘价成交）| 'next_open'
            （与事件引擎撮合时机对齐——首日收盘提交、次日开盘成交;
              供摩擦自洽性检验证明零摩擦下两引擎口径一致, S 验收）。
    """

    def __init__(self, provider: Any, *, bps: float = 10.0) -> None:
        """bps: 调仓费率近似（万分之基点; 10.0 = 0.10% = 每边）。"""
        self._provider = provider
        self._bps = bps

    # ------------------------------------------------------------------
    def run(self, tw: TargetWeights, *, fill_at: str = "close") -> VectorizedResult:
        """跑目标权重序列 → 净值/换手/逐期权重（无订单语义）。"""
        if tw.weights.empty:
            raise MtzQuantError("TargetWeights 为空", stage="vectorized", hint="先构造组合（S3）")
        if fill_at not in ("close", "next_open"):
            raise MtzQuantError(
                f"未知 fill_at {fill_at!r}", stage="vectorized", hint="close|next_open"
            )
        codes = [str(c) for c in tw.weights.columns]
        start = min(d for d in tw.weights.index)
        end = max(d for d in tw.weights.index)
        # 收盘+开盘价矩阵（调仓时点起到数据末尾; 前一日为初始基准）
        closes = self._provider.to_frame(
            codes,
            ["close", "open"],
            self._to_dt(start) - pd.Timedelta(days=3),
            self._to_dt(end) + pd.Timedelta(days=365),
        )
        if closes.empty:
            raise MtzQuantError("无行情数据", stage="vectorized", hint="检查数据覆盖（3.12）")
        dates = closes.index
        tw_dates = {d.date(): tw.weights.loc[d] for d in tw.weights.index if d in dates}
        close_vals = closes[[(c, "close") for c in codes]].to_numpy(dtype=float)
        open_vals = closes[[(c, "open") for c in codes]].to_numpy(dtype=float)
        n, k = close_vals.shape
        held = np.zeros(k, dtype=float)
        nav_arr = np.zeros(n, dtype=float)
        gross_arr = np.zeros(n, dtype=float)
        turnover_out: list[tuple[Any, float]] = []
        nav = 1.0
        gross = 1.0
        pending: np.ndarray | None = None  # next_open: 昨日收盘提交、今日开盘成交的目标权重

        def _apply_rebalance(target: np.ndarray) -> tuple[float, float]:
            """按目标权重换仓; 返回 (成本率, 换手)。"""
            nonlocal held
            s = float(target.sum())
            target = target / s if s > 1e-12 else target
            turnover = float(np.abs(target - held).sum()) / 2.0
            cost = turnover * (self._bps / 10000.0)
            held = target
            return cost, turnover

        for i, ts in enumerate(dates):
            day = ts.date()
            cost_now = 0.0
            if i == 0:
                ret = np.zeros(k, dtype=float)
            elif fill_at == "close":
                px_prev = close_vals[i - 1]
                px_cur = close_vals[i]
                with np.errstate(divide="ignore", invalid="ignore"):
                    ret = np.where(px_prev > 0, px_cur / px_prev - 1.0, 0.0)
            else:  # next_open: 先 旧权重 吃 close_{i-1}→open_i, 开盘换仓, 再 新权重 open_i→close_i
                gap = np.where(close_vals[i - 1] > 0, open_vals[i] / close_vals[i - 1] - 1.0, 0.0)
                nav *= 1.0 + float(held @ gap)
                gross *= 1.0 + float(held @ gap)
                if pending is not None:
                    cost_now, _to = _apply_rebalance(pending)
                    pending = None
                day_part = np.where(open_vals[i] > 0, close_vals[i] / open_vals[i] - 1.0, 0.0)
                ret = day_part
            daily_ret = float(held @ ret)
            nav *= (1.0 + daily_ret) * (1.0 - cost_now)
            gross *= 1.0 + daily_ret
            # 收盘后登记: close 模式今日收盘调仓; next_open 模式今日收盘提交（明日开盘成交）
            if day in tw_dates:
                target = tw_dates[day].reindex(codes).fillna(0.0).to_numpy(dtype=float)
                if fill_at == "close":
                    cost_now, to = _apply_rebalance(target)
                    turnover_out.append((day, to))
                    nav *= 1.0 - cost_now
                else:
                    pending = target.copy()
            nav_arr[i] = nav
            gross_arr[i] = gross
        nav_s = pd.Series(nav_arr, index=dates, dtype=float)
        gross_s = pd.Series(gross_arr, index=dates, dtype=float)
        held_df = pd.DataFrame(0.0, index=dates, columns=codes)
        for i, ts in enumerate(dates):
            if ts.date() in tw_dates and fill_at == "close":
                held_df.loc[ts] = tw_dates[ts.date()].reindex(codes).fillna(0.0).to_numpy()
            elif i > 0:
                prev_w = held_df.iloc[i - 1].to_numpy(dtype=float)
                px_prev = close_vals[i - 1]
                px_cur = close_vals[i]
                with np.errstate(divide="ignore", invalid="ignore"):
                    drift = np.where(px_prev > 0, px_cur / px_prev, 1.0)
                drifted = prev_w * drift
                s = float(drifted.sum())
                held_df.loc[ts] = drifted / s if s > 0 else prev_w
        turnover_s = pd.Series({d: t for d, t in turnover_out}, dtype=float).sort_index()
        return VectorizedResult(
            nav=nav_s,
            gross_nav=gross_s,
            weights_held=held_df,
            turnover=turnover_s,
            data_manifest_hash=tw.data_manifest_hash,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _to_dt(d: Any) -> pd.Timestamp:
        ts = pd.Timestamp(d)
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Shanghai")
        else:
            ts = ts.tz_convert("Asia/Shanghai")
        return ts

    def require_manifest(self, tw: TargetWeights, expected: str) -> None:
        """数据版本锚点一致性断言（S4: 与事件引擎共用数据版本, 5.8）。"""
        if tw.data_manifest_hash and expected and tw.data_manifest_hash != expected:
            raise MtzQuantError(
                "TargetWeights 数据版本与回测数据版本不一致",
                stage="vectorized",
                hint="双引擎必须共用同一 data_manifest_hash（5.8 唯一交接物纪律）",
            )
