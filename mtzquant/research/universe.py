# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 23:55:00
# @update_time        : 2026/08/16 23:55:00
# @description : M3-S2 UniverseEngine：动态股票池（基准池 PIT + 过滤器 + 再平衡, 5.8/3.13）

"""股票池引擎（设计 5.8/3.13）——逐期池 + 每期记录当时可见性依据。

基准池: 指数成分 PIT 展开（R3 `provider.index_stocks`, 取 ≤as_of 最近快照防幸存者偏差）;
        无指数时退化为驱动全部标的。
过滤器链: 上市天数 / 停牌 / ST（v1 用停牌标记代理）/ 退市剔除——每期对每标的判定,
        保留当时可见性依据（basis）供可追溯。
再平衡频率: 周 / 月（calendar 驱动, 确定性 8.8）。

输出: `UniverseSnapshot(date, members, basis)` 列表; 研究层后续构造横截面组合（S3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from mtzquant.core.errors import MtzQuantError


@dataclass(frozen=True)
class UniverseSnapshot:
    """一期股票池（含可见性依据, 3.13 防未来）。"""

    date: date  # 再平衡日（as_of 时点）
    members: list[str]  # 通过过滤器后的标的（升序）
    basis: dict[str, Any] = field(default_factory=dict)  # snapshot_date/过滤器明细


@runtime_checkable
class UniverseFilter(Protocol):
    """过滤器协议: 每标的×时点判定是否入池, 返回 (允许, 原因)。"""

    name: str

    def allowed(self, code: str, dt: date, provider: Any) -> tuple[bool, str]: ...


# ============================================================
# 内置过滤器
# ============================================================
class MinListingDaysFilter:
    """上市天数下限（以该标的可见历史 bar 数代理, 3.13 可见性）。"""

    name = "min_listing_days"

    def __init__(self, n: int = 60) -> None:
        self.n = n

    def allowed(self, code: str, dt: date, provider: Any) -> tuple[bool, str]:
        arr = provider.bar_array(code)
        if arr.size == 0:
            return False, "no_data"
        # 可见历史 = ≤dt 的 bar 数
        cutoff = datetime(dt.year, dt.month, dt.day, 15, 0).timestamp() * 1000
        idx = int(__import__("numpy").searchsorted(arr["dt"], int(cutoff), side="right"))
        if idx < self.n:
            return False, f"listing_days<{self.n}"
        return True, "ok"


class SuspendedFilter:
    """停牌剔除（ST 暂用停牌标记代理, v1 约定）: 当日无交易标记 → 不入池。"""

    name = "not_suspended"

    def allowed(self, code: str, dt: date, provider: Any) -> tuple[bool, str]:
        from mtzquant.data.provider import _at_shanghai

        bar = provider.bar_at(code, _at_shanghai(dt))
        if bar is None:
            return False, "no_bar"
        if bar.suspended:
            return False, "suspended"
        return True, "ok"


class DelistedFilter:
    """退市剔除: master delist_date 已过 或 当日无 bar（数据缺失按不可交易处理）。"""

    name = "not_delisted"

    def allowed(self, code: str, dt: date, provider: Any) -> tuple[bool, str]:
        from mtzquant.data.provider import _at_shanghai

        bar = provider.bar_at(code, _at_shanghai(dt))
        if bar is None:
            return False, "delisted_or_missing"
        return True, "ok"


# ============================================================
# 引擎
# ============================================================
class UniverseEngine:
    """动态股票池（基准池 PIT + 过滤器链 + 再平衡频率, 5.8/3.13）。"""

    def __init__(
        self,
        provider: Any,
        *,
        index_code: str | None = None,
        filters: list[UniverseFilter] | None = None,
        rebalance: str = "M",  # W=周, M=月
    ) -> None:
        if rebalance not in ("W", "M"):
            raise MtzQuantError(
                f"未知再平衡频率 {rebalance!r}", stage="universe", hint="W（周）| M（月）"
            )
        self._provider = provider
        self._index = index_code
        self._filters = filters if filters is not None else [SuspendedFilter()]
        self._rebalance = rebalance

    # ------------------------------------------------------------------
    def rebalance_dates(self, start: date, end: date) -> list[date]:
        """再平衡日（周/月, calendar 驱动, 确定性 8.8）。"""
        days = self._provider.trading_days(start, end)
        if self._rebalance == "W":
            return days[::5] or (days[:1] if days else [])
        # 月: 每月首个交易日
        out: list[date] = []
        seen: set[tuple[int, int]] = set()
        for d in days:
            key = (d.year, d.month)
            if key not in seen:
                seen.add(key)
                out.append(d)
        return out

    # ------------------------------------------------------------------
    def run(self, start: date, end: date) -> list[UniverseSnapshot]:
        """逐期构建股票池（基准池 as_of + 过滤器链）。"""
        periods = self.rebalance_dates(start, end)
        if not periods:
            return []
        base = self._all_instruments() if self._index is None else None
        snapshots: list[UniverseSnapshot] = []
        for pdate in periods:
            as_of = datetime(pdate.year, pdate.month, pdate.day, 15, 0)
            if self._index is not None:
                pool = self._provider.index_stocks(self._index, as_of)
                basis: dict[str, Any] = {"index": self._index, "as_of": pdate.isoformat()}
            else:
                pool = list(base or [])
                basis = {"index": None, "as_of": pdate.isoformat()}
            members: list[str] = []
            reasons: dict[str, str] = {}
            for code in sorted(pool):
                ok, reason = self._allowed(code, pdate)
                if ok:
                    members.append(code)
                else:
                    reasons[code] = reason
            basis["filters"] = {f.name: "applied" for f in self._filters}
            basis["rejected"] = reasons
            snapshots.append(UniverseSnapshot(date=pdate, members=sorted(members), basis=basis))
        return snapshots

    # ------------------------------------------------------------------
    def _allowed(self, code: str, dt: date) -> tuple[bool, str]:
        for f in self._filters:
            ok, reason = f.allowed(code, dt, self._provider)
            if not ok:
                return False, f"{f.name}:{reason}"
        return True, "ok"

    def _all_instruments(self) -> list[str]:
        return self._provider.all_codes()
