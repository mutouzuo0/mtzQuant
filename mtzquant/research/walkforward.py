# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 01:20:00
# @update_time        : 2026/08/17 01:20:00
# @description : M3-U4 WalkForward：滚动窗口编排（train→test 逐段推进, 设计 5.8.2）

"""Walk-Forward 编排器（设计 5.8.2, M3-U4）——滚动 train→test 逐段推进。

- `WalkForwardRunner.run(days, fit_fn, evaluate_fn)`: 每段 train 窗口拟合参数,
  test 窗口评估 OOS——输出逐段 OOS 净值、逐段参数漂移表;
- 拼接: 各段 OOS 净值首尾相接（后段净值 × 前段末值, 8.8 确定性）;
- 参数漂移表: 逐段参数展示（稳定性目视/自动检查, T-F04）。

fit_fn(train_days) -> params; evaluate_fn(params, test_days) -> (oos_nav, metrics)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from mtzquant.core.errors import MtzQuantError


@dataclass
class WalkForwardWindow:
    """单段 walk-forward（train 窗口拟合 → test 窗口 OOS 评估）。"""

    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    params: dict[str, Any] = field(default_factory=dict)
    oos_metrics: dict[str, float] = field(default_factory=dict)
    oos_nav: pd.Series | None = None  # 该段 OOS 净值（首日=1.0）


@dataclass
class WalkForwardResult:
    """walk-forward 全量产出（拼接净值 + 参数漂移表）。"""

    windows: list[WalkForwardWindow] = field(default_factory=list)
    oos_nav_concatenated: pd.Series | None = None
    param_drift: pd.DataFrame | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "n_windows": len(self.windows),
            "param_drift": (
                self.param_drift.to_dict(orient="index") if self.param_drift is not None else None
            ),
            "windows": [
                {
                    "index": w.index,
                    "train": [w.train_start.isoformat(), w.train_end.isoformat()],
                    "test": [w.test_start.isoformat(), w.test_end.isoformat()],
                    "params": w.params,
                    "oos_metrics": w.oos_metrics,
                    "oos_nav_end": float(w.oos_nav.iloc[-1])
                    if w.oos_nav is not None and len(w.oos_nav)
                    else None,
                }
                for w in self.windows
            ],
        }


class WalkForwardRunner:
    """滚动窗口编排器（5.8.2, U4）。"""

    def __init__(
        self,
        *,
        train_bars: int = 60,
        test_bars: int = 20,
        step: int | None = None,
        embargo_bars: int = 0,
    ) -> None:
        if train_bars < 1 or test_bars < 1:
            raise MtzQuantError(
                f"窗口参数非法: train={train_bars}, test={test_bars}",
                stage="walkforward",
            )
        self.train_bars = train_bars
        self.test_bars = test_bars
        self.step = step or test_bars  # 滚动步长（默认 = test 窗口, 无重叠）
        self.embargo_bars = embargo_bars

    # ------------------------------------------------------------------
    def run(
        self,
        days: list[date],
        fit_fn: Callable[[list[date]], dict[str, Any]],
        evaluate_fn: Callable[[dict[str, Any], list[date]], tuple[pd.Series, dict[str, float]]],
    ) -> WalkForwardResult:
        """逐段推进: train 拟合 → test OOS 评估 → 拼接净值 + 参数漂移表。"""
        seq = sorted(days)
        if len(seq) < self.train_bars + self.test_bars + 1:
            raise MtzQuantError(
                f"数据太短（{len(seq)} < train {self.train_bars} + test {self.test_bars}）",
                stage="walkforward",
            )
        windows: list[WalkForwardWindow] = []
        idx = 0
        while idx + self.train_bars + self.test_bars <= len(seq):
            train_win = seq[idx : idx + self.train_bars]
            test_win = seq[idx + self.train_bars : idx + self.train_bars + self.test_bars]
            # embargo: 训练窗口上界前移（防 horizon 标签跨越泄露, U2 语义）
            fit_days = (
                train_win[: len(train_win) - self.embargo_bars]
                if self.embargo_bars > 0
                else train_win
            )
            params = fit_fn(list(fit_days))
            oos_nav, metrics = evaluate_fn(params, list(test_win))
            windows.append(
                WalkForwardWindow(
                    index=idx,
                    train_start=train_win[0],
                    train_end=train_win[-1],
                    test_start=test_win[0],
                    test_end=test_win[-1],
                    params=dict(params),
                    oos_metrics=dict(metrics),
                    oos_nav=oos_nav,
                )
            )
            idx += self.step
        if not windows:
            raise MtzQuantError("无完整 walk-forward 窗口", stage="walkforward")
        result = WalkForwardResult(windows=windows)
        result.oos_nav_concatenated = self._concat(windows)
        result.param_drift = self._drift_table(windows)
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _concat(windows: list[WalkForwardWindow]) -> pd.Series:
        """各段 OOS 净值首尾相接（后段 × 前段末值, 确定性 8.8）。"""
        parts: list[pd.Series] = []
        cum = 1.0
        for w in windows:
            nav = w.oos_nav
            if nav is None or len(nav) == 0:
                continue
            nav = nav / float(nav.iloc[0])  # 段内归一
            parts.append(nav * cum)
            cum *= float(nav.iloc[-1])
        if not parts:
            return pd.Series(dtype=float)
        return pd.concat(parts)

    @staticmethod
    def _drift_table(windows: list[WalkForwardWindow]) -> pd.DataFrame:
        """逐段参数漂移表（index=窗口, columns=参数）。"""
        if not windows:
            return pd.DataFrame()
        df = pd.DataFrame([w.params for w in windows], index=[w.index for w in windows])
        return df
