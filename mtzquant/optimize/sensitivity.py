# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:50:00
# @update_time        : 2026/08/17 00:50:00
# @description : M3-T4 邻域敏感性：胜出参数 ±10%/±1 档强制扫描（5.8.2 防过拟合）

"""邻域敏感性（设计 5.8.2, M3-T4）——胜出参数稳定性的强制门禁。

- `build_neighborhood(params, step_ratio)`: 每参数 ±1 档/±10% 邻域（单参数逐扰, 防组合爆炸;
  邻域用向量化引擎跑, 成本可控——§8 风险对策）;
- `SensitivityReport`: 邻域指标分布表 + 峰值判别（阈值可配）;
- **不出邻域报告的胜出参数不允许标记 adopted**（门禁: `adopted_allowed()`）。

峰值判别: 邻域最佳指标显著优于基线（相对超幅 > peak_threshold）→ 尖锐峰值（脆弱最优,
优化过拟合信号）; 或邻域波动过大 → 同样不可 adopted。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mtzquant.core.errors import MtzQuantError


def build_neighborhood(
    params: dict[str, Any], *, step_ratio: float = 0.10, min_step: int = 1
) -> list[dict[str, Any]]:
    """单参数 ±1 档/±10% 邻域（每参数独立扰动, 其余保持基线）。

    int 参数: ±max(min_step, round(ratio*param)); float 参数: ±ratio*param;
    布尔/字符串参数跳过（无自然邻域）。
    """
    if not 0 < step_ratio < 1:
        raise MtzQuantError(f"step_ratio 须在 (0,1), 得到 {step_ratio}", stage="sensitivity")
    neighbors: list[dict[str, Any]] = []
    for key, value in sorted(params.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        step = _step(value, step_ratio, min_step)
        if step == 0:
            continue
        for sign in (-1, 1):
            nv = value + sign * step
            if nv <= 0:  # 参数须为正（fast/slow 均正; 0/负无意义）
                continue
            nb = dict(params)
            nb[key] = round(nv) if isinstance(value, int) else round(nv, 6)
            neighbors.append(nb)
    if not neighbors:
        raise MtzQuantError(
            f"参数 {sorted(params)} 无数值型可扰动项", stage="sensitivity", hint="需 int/float 参数"
        )
    return neighbors


def _step(value: int | float, ratio: float, min_step: int) -> int | float:
    if isinstance(value, int):
        return max(min_step, round(ratio * value))
    return round(ratio * value, 6) or min_step


@dataclass
class SensitivityReport:
    """邻域敏感性报告（5.8.2; 峰值判别阈值可配）。"""

    base_params: dict[str, Any]
    neighbors: list[dict[str, Any]] = field(default_factory=list)
    base_metric: float = 0.0
    neighbor_metrics: dict[str, float] = field(default_factory=dict)
    peak_threshold: float = 0.25  # 邻域最佳相对基线超幅阈值（可配, 5.8.2）
    best_neighbor: dict[str, Any] = field(default_factory=dict)
    best_neighbor_metric: float = 0.0

    @property
    def peak_ratio(self) -> float:
        """邻域最佳/基线超幅（0 = 无超幅; >threshold → 尖锐峰值）。"""
        if self.base_metric == 0:
            return 0.0
        return round((self.best_neighbor_metric - self.base_metric) / abs(self.base_metric), 6)

    @property
    def sharp_peak(self) -> bool:
        """是否尖锐峰值（邻域显著优于基线 → 参数脆弱, 过拟合信号）。"""
        return self.peak_ratio > self.peak_threshold

    def adopted_allowed(self) -> bool:
        """门禁: 不出邻域报告 / 尖锐峰值 → 不允许标记 adopted（5.8.2）。"""
        if not self.neighbors:
            return False
        return not self.sharp_peak

    # ------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "base_params": self.base_params,
            "base_metric": self.base_metric,
            "peak_threshold": self.peak_threshold,
            "peak_ratio": self.peak_ratio,
            "sharp_peak": self.sharp_peak,
            "adopted_allowed": self.adopted_allowed(),
            "neighbors": [
                {"params": nb, "metric": self.neighbor_metrics.get(_key(nb), None)}
                for nb in self.neighbors
            ],
        }

    def to_markdown(self) -> str:
        lines = [
            "# 邻域敏感性报告",
            "",
            f"- 基线参数: `{self.base_params}` → 指标 {self.base_metric:.4f}",
            f"- 邻域最佳: `{self.best_neighbor}` → {self.best_neighbor_metric:.4f}",
            f"- 峰值超幅: **{self.peak_ratio:.4f}**（阈值 {self.peak_threshold}）",
            f"- 尖锐峰值: {'⚠️ 是' if self.sharp_peak else '否'}",
            f"- adopted 许可: {'✅' if self.adopted_allowed() else '🚫 拒绝（需复查/淘汰）'}",
            "",
            "| 参数组 | 指标 |",
            "|--------|------|",
        ]
        for nb in self.neighbors:
            lines.append(f"| `{nb}` | {self.neighbor_metrics.get(_key(nb), float('nan')):.4f} |")
        return "\n".join(lines)


def _key(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True)


def scan_sensitivity(
    base_params: dict[str, Any],
    metric_fn: Callable[[dict[str, Any]], float],
    *,
    step_ratio: float = 0.10,
    peak_threshold: float = 0.25,
) -> SensitivityReport:
    """扫描邻域指标（5.8.2）; metric_fn(params) -> 指标（向量化引擎跑, 成本可控）。"""
    neighbors = build_neighborhood(base_params, step_ratio=step_ratio)
    report = SensitivityReport(
        base_params=dict(base_params),
        neighbors=neighbors,
        base_metric=float(metric_fn(base_params)),
        peak_threshold=peak_threshold,
    )
    best_metric = report.base_metric
    best_nb: dict[str, Any] = {}
    for nb in neighbors:
        m = float(metric_fn(nb))
        report.neighbor_metrics[_key(nb)] = m
        if m > best_metric:
            best_metric = m
            best_nb = nb
    report.best_neighbor_metric = best_metric
    report.best_neighbor = best_nb or dict(base_params)
    return report
