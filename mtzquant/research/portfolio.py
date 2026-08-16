# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:00:00
# @update_time        : 2026/08/17 00:00:00
# @description : M3-S3 PortfolioConstructor + TargetWeights（双引擎唯一交接物, 设计 5.8/5.8.1）

"""横截面组合构造（设计 5.8/5.8.1）——双引擎交接物的唯一来源。

TargetWeights: 调仓时点×标的 → 权重, 附 data_manifest_hash 数据版本锚点;
               **事件驱动引擎与向量化引擎之间唯一交接格式**（禁止第二种, 5.8）。

PortfolioConstructor:
  打分（因子 Z 分/排名, 5.8.1）→ 加权（等权/市值权/打分 TopN）→ 约束
  （max_weight/min_weight/top_n/long_only）; 逐期权重满足约束（T-V03 断言）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from mtzquant.core.errors import MtzQuantError


@dataclass(frozen=True)
class TargetWeights:
    """双引擎唯一交接物（4.2 冻结接口）: index=调仓时点, columns=code, values=权重。

    data_manifest_hash: 数据版本锚点——产物无版本锚点禁止入库（M3 §4.3 纪律）。
    """

    weights: pd.DataFrame
    data_manifest_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.weights, pd.DataFrame) or self.weights.empty:
            raise MtzQuantError(
                "TargetWeights.weights 必须为非空 DataFrame（时点×标的→权重）",
                stage="portfolio",
                hint="用 PortfolioConstructor.build() 构造",
            )

    # ------------------------------------------------------------------
    def codes(self) -> list[str]:
        return [str(c) for c in self.weights.columns]

    def dates(self) -> list[Any]:
        return list(self.weights.index)

    def rebalance_turnover(self, prev: TargetWeights | None = None) -> pd.Series:
        """每期目标权重相对上一期目标权重的换手（Σ|Δw|/2, 供 S4 成本估算）。"""
        cur = self.weights.fillna(0.0)
        if prev is None:
            base = pd.DataFrame(0.0, index=cur.index, columns=cur.columns)
        else:
            base = prev.weights.reindex(index=cur.index, columns=cur.columns).fillna(0.0)
        return (cur - base).abs().sum(axis=1) / 2.0

    def to_json(self) -> dict[str, Any]:
        return {
            "weights": self.weights.to_dict(orient="index"),
            "data_manifest_hash": self.data_manifest_hash,
        }


@dataclass
class Constraints:
    """组合约束（5.8.1）: 权重上限/下限 / Top-N / 仅多仓。"""

    max_weight: float = 1.0
    min_weight: float = 0.0  # < min_weight 的标的剔除（含 0）
    top_n: int | None = None  # 只保留打分前 N
    long_only: bool = True  # 负权重剔除


# ============================================================
# 打分（Z 分/排名, 5.8.1）
# ============================================================
def zscore_cross_section(values: pd.DataFrame) -> pd.DataFrame:
    """横截面 Z 分（每期逐行标准化）; 空/零标准差期 → NaN（调用方剔除）。"""
    mean = values.mean(axis=1)
    std = values.std(axis=1)
    out = values.sub(mean, axis=0).div(std.replace(0.0, float("nan")), axis=0)
    return out


def rank_cross_section(values: pd.DataFrame, ascending: bool = True) -> pd.DataFrame:
    """横截面排名（每期逐行 rank, 1=最好; NaN 保留）。"""
    return values.rank(axis=1, ascending=ascending, method="average")


# ============================================================
# 组合构造器
# ============================================================
class PortfolioConstructor:
    """打分 → 加权 → 约束 → TargetWeights（5.8.1）。"""

    def __init__(self, provider: Any | None = None) -> None:
        self._provider = provider  # 市值权需要（可 None）

    def build(
        self,
        scores: pd.DataFrame,
        universe: list[Any] | dict[Any, list[str]] | None = None,
        constraints: Constraints | None = None,
        *,
        method: str = "score_topn",
        data_manifest_hash: str = "",
    ) -> TargetWeights:
        """构造目标权重。

        scores:      index=调仓时点, columns=候选 code, values=打分（越高越好）;
        universe:    逐期池（S2 输出列表/映射）; None → 用 scores 列全量;
        method:      equal（等权）| market_cap（市值权）| score_topn（打分 TopN 加权）。
        """
        constraints = constraints or Constraints()
        if scores.empty:
            raise MtzQuantError("scores 为空", stage="portfolio", hint="先计算因子打分")
        if method not in ("equal", "market_cap", "score_topn"):
            raise MtzQuantError(
                f"未知加权方法 {method!r}", stage="portfolio", hint="equal|market_cap|score_topn"
            )
        members = self._members_per_period(scores.index, universe)
        out = pd.DataFrame(0.0, index=scores.index, columns=sorted(scores.columns))
        for dt, idx in enumerate(scores.index):
            pool = members.get(scores.index[dt]) if isinstance(members, dict) else members
            period_scores = scores.loc[idx].dropna()
            cand = (
                [c for c in pool if c in period_scores.index] if pool else list(period_scores.index)
            )
            if not cand:
                continue
            w = self._weight_period(period_scores, cand, method, constraints)
            for code, value in w.items():
                out.at[idx, code] = value
        out = self._apply_constraints(out, constraints)
        # 逐期归一（权重和 ≤ 1, 现金隐含余量）
        out = out.div(out.sum(axis=1).replace(0.0, float("nan")), axis=0).fillna(0.0)
        return TargetWeights(weights=out, data_manifest_hash=data_manifest_hash)

    # ------------------------------------------------------------------
    @staticmethod
    def _members_per_period(
        dates: pd.Index, universe: list[Any] | dict[Any, list[str]] | None
    ) -> dict[Any, list[str]] | None:
        """把 S2 快照列表/映射归一为 {date: [codes]}; None → 全量。"""
        if universe is None:
            return None
        if isinstance(universe, dict):
            return {k: list(v) for k, v in universe.items()}
        out: dict[Any, list[str]] = {}
        for item in universe:
            d = getattr(item, "date", None)
            m = getattr(item, "members", None)
            if d is not None and m is not None:
                out[d] = list(m)
        return out or None

    def _weight_period(
        self, scores: pd.Series, cand: list[str], method: str, c: Constraints
    ) -> dict[str, float]:
        s = scores.loc[cand]
        if method == "equal":
            n = len(cand)
            return {code: 1.0 / n for code in cand}
        if method == "market_cap":
            caps = self._market_caps(cand, scores)
            total = float(sum(caps.values()))
            if total <= 0:
                n = len(cand)
                return {code: 1.0 / n for code in cand}
            return {code: caps[code] / total for code in cand}
        # score_topn: Top-N 后按正打分加权（负分剔除, 5.8.1）
        if c.top_n is not None and len(cand) > c.top_n:
            top = s.nlargest(c.top_n).index.tolist()
        else:
            top = s[s > 0].index.tolist() or cand
        pos = s.loc[top]
        if pos.sum() <= 0:
            n = len(top)
            return {code: 1.0 / n for code in top}
        return {code: float(pos[code] / pos.sum()) for code in top}

    def _market_caps(self, cand: list[str], scores: pd.Series) -> dict[str, float]:
        """逐标的市值（Provider fundamentals daily_basic.total_mv, as-of=打分期）。"""
        if self._provider is None:
            raise MtzQuantError(
                "市值权需要 provider 注入（PortfolioConstructor(provider=...)）",
                stage="portfolio",
            )
        from datetime import datetime
        from zoneinfo import ZoneInfo

        dt = scores.name
        as_of = datetime(dt.year, dt.month, dt.day, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        caps: dict[str, float] = {}
        for code in cand:
            rows = self._provider.fundamentals(code, "daily_basic", ["total_mv"], as_of=as_of)
            if rows is None or rows.empty:
                continue
            v = float(rows["total_mv"].iloc[-1])
            if v == v and v > 0:  # NaN 守卫
                caps[code] = v
        return caps

    @staticmethod
    def _apply_constraints(out: pd.DataFrame, c: Constraints) -> pd.DataFrame:
        """约束: 仅多仓 / 下限剔除 / 上限封顶后溢出重归一。"""
        if c.long_only:
            out = out.clip(lower=0.0)
        if c.min_weight > 0:
            out = out.where(out >= c.min_weight, 0.0)
        if c.max_weight < 1.0:
            cap = float(c.max_weight)
            for idx in out.index:
                row = out.loc[idx]
                if float(row.max()) > cap:
                    clipped = row.clip(upper=cap)
                    excess = float(row.sum() - clipped.sum())
                    under = clipped[clipped < cap]
                    if len(under):
                        clipped[under.index] += excess * (under / under.sum())
                    out.loc[idx] = clipped
        return out
