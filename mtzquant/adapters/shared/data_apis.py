# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:58:00
# @update_time        : 2026/08/16 21:59:08
# @description : K6 数据族 PIT 核心（4.6/3.13）：provider 包装——as_of/knowledge_time/快照

"""数据族 PIT 核心（设计 3.13/4.6）——`history/attribute_history/get_price/current_data`。

- PIT 强制: as_of 缺省取当前回测时刻; knowledge_time = as_of（无未来数据泄露, T-A06）;
- include_today 缺省语义: 盘前(false, 当日 bar 不可见) / 收盘(true, 当日已定稿), 与 5.2 一致;
- 平台参数差异（fields 缺省/df 布尔/单位/fq）由各适配器在其命名空间里适配, 核心只做 PIT 正确查询;
- `current_data` 快照对象工厂: 字段集由各平台 BarData 投影决定（本核心给原始字段）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from mtzquant.core.types import KLINE_COLUMNS
from mtzquant.data.provider import MarketDataProvider


def _at15(dt: datetime | date) -> datetime:
    """日线 bar 时点（15:00, 8.8 确定性）。"""
    if isinstance(dt, datetime):
        return dt.replace(hour=15, minute=0, second=0, microsecond=0)
    return datetime(dt.year, dt.month, dt.day, 15, 0)


def _ts(dt: datetime | date) -> Any:
    """→ Asia/Shanghai 时区 Timestamp（provider.history 索引口径, 3.13）。"""
    import pandas as pd

    if isinstance(dt, datetime):
        naive = dt.replace(tzinfo=None)
    else:
        naive = datetime(dt.year, dt.month, dt.day)
    return pd.Timestamp(naive, tz="Asia/Shanghai")


class DataApiCore:
    """PIT 正确数据查询核心（会话注入 provider + 当前时点/阶段）。"""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        current_dt: Callable[[], datetime | None],
        phase: Callable[[], str],
    ) -> None:
        self._provider = provider
        self._current_dt = current_dt
        self._phase = phase

    # ------------------------------------------------------------------
    def _as_of(self, as_of: datetime | date | None) -> datetime:
        if isinstance(as_of, date) and not isinstance(as_of, datetime):
            return _at15(as_of)
        if isinstance(as_of, datetime):
            return _at15(as_of)
        now = self._current_dt()
        if now is None:
            raise RuntimeError("数据查询只能在回测运行期内调用（策略回调中）")
        return _at15(now)

    def _include_today(self, include_today: bool | None) -> bool:
        if include_today is not None:
            return include_today
        # 盘前不含当日, 收盘含当日（5.2 可见性）
        return self._phase() == "on_daily_close"

    # ------------------------------------------------------------------
    def history(
        self,
        code: str,
        count: int,
        *,
        unit: str = "1d",
        fields: list[str] | None = None,
        include_today: bool | None = None,
        as_of: datetime | date | None = None,
    ) -> Any:
        """最近 count 根可见 bar（DataFrame, 索引=UTC+8 dt）。"""
        from mtzquant.core.types import Frequency

        return self._provider.history(
            code,
            fields or list(KLINE_COLUMNS),
            count,
            as_of=self._as_of(as_of),
            knowledge_time=self._as_of(as_of),
            include_today=self._include_today(include_today),
            frequency=Frequency(unit),
        )

    def attribute_history(
        self,
        code: str,
        count: int,
        *,
        unit: str = "1d",
        fields: list[str] | None = None,
        skip_paused: bool = True,
        include_today: bool | None = None,
    ) -> Any:
        """聚宽 attribute_history（与 history 同源; skip_paused 由数据侧停牌标记承载）。"""
        return self.history(code, count, unit=unit, fields=fields, include_today=include_today)

    def get_price(
        self,
        security: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        count: int | None = None,
        unit: str = "1d",
        fields: list[str] | None = None,
        include_today: bool | None = None,
    ) -> Any:
        """区间/根数行情（日线; count 优先, 否则按 [start,end] 过滤）。"""
        n = count or 3000  # 日线窗口内足够大（M2 无日历依赖; M4 下载页按日历精确化）
        df = self.history(
            security,
            n,
            unit=unit,
            fields=fields,
            include_today=include_today,
            as_of=end_date,
        )
        if df is None or getattr(df, "empty", True):
            return df
        index = df.index
        if start_date is not None:
            df = df[index >= _ts(start_date)]
        if end_date is not None:
            df = df[index <= _ts(_at15(end_date))]
        return df

    # ------------------------------------------------------------------
    def bar(self, code: str, as_of: datetime | date | None = None) -> Any:
        """当日 bar（PIT: as_of 时点 15:00; 停牌/缺失 → None）。"""
        return self._provider.bar_at(code, self._as_of(as_of))

    def current_data(self, security_list: list[str] | None = None) -> dict[str, SimpleNamespace]:
        """data[security] 快照对象（原始字段; 平台字段映射由适配器包装, 4.6）。"""
        out: dict[str, SimpleNamespace] = {}
        for code in sorted(security_list or []):
            bar = self.bar(code)
            out[code] = SimpleNamespace(
                code=code,
                last_price=float(bar.close) if bar else 0.0,
                open=float(bar.open) if bar else 0.0,
                high=float(bar.high) if bar else 0.0,
                low=float(bar.low) if bar else 0.0,
                close=float(bar.close) if bar else 0.0,
                volume=float(bar.volume) if bar else 0.0,
                amount=float(bar.amount) if bar else 0.0,
                pre_close=float(bar.pre_close) if bar else 0.0,
                paused=bool(bar.suspended) if bar else True,
                limit_up=float(bar.limit_up) if bar else 0.0,
                limit_down=float(bar.limit_down) if bar else 0.0,
            )
        return out
