# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:38:00
# @update_time        : 2026/08/16 21:59:08
# @description : D6 MarketDataProvider：PIT 时点 + BarArray 二分（3.7/3.8/3.13）

"""统一数据供给层（设计 3.7/3.8/3.13）——三段式管道第 ③ 段。

职责: 按「可见时间」安全供给。所有查询强制 as_of/knowledge_time 双时点:
    历史理想回测 knowledge_time=as_of（行情类 event_time==published_at, 单参即可）。
热路径: {code: BarArray}（numpy 结构化数组）+ dt 二分定位 → O(log n) bar_at;
        history() 批量窗口切片（视图级, 不逐行 pandas 查询）。
性能占位: to_frame()/to_numpy()（设计 3.8, M3 实现）——签名就位、为空实现。
预加载: preload_mode ∈ window(默认, 池+区间+预热期)/all(全量)/lazy(全惰性);
        warmup_bars 默认 120（覆盖 MA250 等指标回看, 3.7）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.pit import PitQuery
from mtzquant.core.types import AdjustMode, Frequency
from mtzquant.data.cache import DataCache
from mtzquant.data.calendar import TradeCalendar
from mtzquant.data.drivers.base import SourceDriver
from mtzquant.data.normalizer import DataNormalizer, dt_index_to_ms


class Bar(NamedTuple):
    """单个 bar（bar_at 的返回值; dt 为 UTC+8 毫秒整数, 纪律 8.8）。"""

    dt: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    pre_close: float
    suspended: int
    limit_up: float
    limit_down: float


# numpy 结构化数组 dtype（与 KLINE_COLUMNS 顺序一致; dt 即日期索引, 已排序可二分）
dtype_fields: list[tuple[str, str]] = [
    ("dt", "i8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("volume", "f8"),
    ("amount", "f8"),
    ("pre_close", "f8"),
    ("suspended", "i8"),
    ("limit_up", "f8"),
    ("limit_down", "f8"),
]
BAR_DTYPE = np.dtype(dtype_fields)
# 字段名元组（dtype.names 在 numpy 类型上可为 None, 此处显式非空）
BAR_NAMES: tuple[str, ...] = tuple(BAR_DTYPE.names or ())


class MarketDataProvider:
    """PIT 供给层（设计 3.7/3.13）。"""

    def __init__(
        self,
        driver: SourceDriver,
        calendar: TradeCalendar,
        *,
        normalizer: DataNormalizer | None = None,
        cache: DataCache | None = None,
        preload_mode: str = "window",
        warmup_bars: int = 120,
    ) -> None:
        self._driver = driver
        self._calendar = calendar
        self._normalizer = normalizer or DataNormalizer()
        self._cache = cache  # L2 加速（可 None）
        self._preload_mode = preload_mode
        self._warmup_bars = warmup_bars
        self._arrays: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # 加载（懒加载 + 预加载）
    # ------------------------------------------------------------------
    def _load(
        self, code: str, frequency: Frequency = Frequency.D1, adjust: AdjustMode = AdjustMode.NONE
    ) -> np.ndarray:
        """加载/缓存标的 → numpy 结构化数组（L1 内存常驻, 3.7）。"""
        if code in self._arrays:
            return self._arrays[code]

        def loader() -> pd.DataFrame:
            raw = self._driver.load_kline(
                code, frequency, datetime(1970, 1, 1), datetime(2100, 1, 1), adjusted=adjust
            )
            return self._normalizer.normalize(raw, code, fmt="auto", limit=None)

        if self._cache is not None:
            df = self._cache.get(
                code, frequency, adjust, loader=loader, source_ref=self._source_path(code)
            )
        else:
            df = loader()
        arr = df_to_bar_array(df)
        self._arrays[code] = arr
        return arr

    def _source_path(self, code: str) -> Any | None:
        """源 CSV 路径（L2 失效判据, 3.7 mtime/size; 非本地 CSV 驱动 → None 永不失效）。"""
        driver = getattr(self._driver, "kline_path", None)
        if driver is None:
            return None
        try:
            return driver(code, Frequency.D1)
        except MtzQuantError:
            return None

    def preload(
        self, codes: list[str], start: datetime, end: datetime, frequency: Frequency = Frequency.D1
    ) -> None:
        """预加载: 池+区间(+预热段) 一次性入内存（window 模式, 3.7）。

        lazy 模式为 no-op（首次访问才加载）; all/window 均按 codes 全量载入内存——
        v1 日线数据量小, 加载后即 numpy 常驻, 主循环零解析（热路径, 3.7）。
        区间裁剪在查询期由 PIT 时点完成, 预加载不做数据裁剪（L2 parquet 已按标的切片）。
        """
        del start, end  # 语义: 预加载区间由查询期 PIT 控制, 这里仅作池接口文档
        if self._preload_mode == "lazy":
            return
        for code in sorted(set(codes)):
            self._load(code, frequency)

    # ------------------------------------------------------------------
    # PIT 查询（3.13）
    # ------------------------------------------------------------------
    def _visible_slice(self, arr: np.ndarray, q: PitQuery, include_today: bool) -> slice:
        """行情类可见窗口（event_time==published_at → 可见 iff dt <= min(as_of, knowledge_time)）。

        include_today=False（默认）: bar 事件时刻 <= 截止（当日 15:00 收盘 bar 在
            盘前/as_of 早于收盘时不吸入——防未来数据, 3.13）。
        include_today=True      : 允许 as_of 当日整日可见（盘后 on_daily_close 场景:
            账户已含当日成交, 当日收盘 bar 对策略可见, 5.1）。
        """
        cutoff_ms = int(min(q.as_of, q.knowledge_time or q.as_of).timestamp() * 1000)
        if include_today:
            day_start = datetime(
                q.as_of.year, q.as_of.month, q.as_of.day, tzinfo=ZoneInfo("Asia/Shanghai")
            )
            cutoff_ms = int(day_start.timestamp() * 1000) + 86_400_000 - 1
        idx = int(np.searchsorted(arr["dt"], cutoff_ms, side="right"))
        return slice(0, idx)

    def history(
        self,
        code: str,
        fields: list[str] | None,
        n: int,
        *,
        as_of: datetime,
        knowledge_time: datetime | None = None,
        include_today: bool = False,
        frequency: Frequency = Frequency.D1,
    ) -> pd.DataFrame:
        """可见时间窗口内最近 n 根 bar（PIT 强制, 设计 3.13）。"""
        if n < 1:
            raise MtzQuantError(
                f"history n 必须 >= 1，得到 {n}", stage="provider", hint="n 为回看根数"
            )
        arr = self._load(code, frequency)
        q = PitQuery(as_of=as_of, knowledge_time=knowledge_time)
        window = arr[self._visible_slice(arr, q, include_today)]
        tail = window[-n:] if window.size else window
        return array_to_frame(tail, fields)

    def bar_at(self, code: str, dt: datetime, frequency: Frequency = Frequency.D1) -> Bar | None:
        """热路径: 二分定位（O(log n)）返回单根 bar; 无匹配 → None。"""
        arr = self._load(code, frequency)
        target_ms = int(dt.timestamp() * 1000)
        idx = int(np.searchsorted(arr["dt"], target_ms, side="left"))
        if idx >= arr.size or arr["dt"][idx] != target_ms:
            return None
        row = arr[idx]
        return Bar(
            dt=int(row["dt"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            amount=float(row["amount"]),
            pre_close=float(row["pre_close"]),
            suspended=int(row["suspended"]),
            limit_up=float(row["limit_up"]),
            limit_down=float(row["limit_down"]),
        )

    def bar_array(self, code: str, frequency: Frequency = Frequency.D1) -> np.ndarray:
        """暴露内部 BarArray（引擎/指标共用热路径）。"""
        return self._load(code, frequency)

    # ------------------------------------------------------------------
    # 预留（设计 3.8, M3 实现）
    # ------------------------------------------------------------------
    def to_frame(
        self, codes: list[str], fields: list[str], start: datetime, end: datetime
    ) -> pd.DataFrame:  # pragma: no cover
        raise MtzQuantError(
            "provider.to_frame 未实现（M3）",
            stage="provider",
            hint="批量宽表读取属研究引擎（M3）；回测热路径用 history()/bar_at()",
        )

    def to_numpy(
        self, codes: list[str], fields: list[str], start: datetime, end: datetime
    ) -> np.ndarray:  # pragma: no cover
        raise MtzQuantError(
            "provider.to_numpy 未实现（M3）",
            stage="provider",
            hint="矩阵输出供 torch/sklearn；M3 VectorizedBacktester 接入",
        )


# ------------------------------------------------------------------
# 转换工具
# ------------------------------------------------------------------
def df_to_bar_array(df: pd.DataFrame, fields: tuple[str, ...] | None = None) -> np.ndarray:
    """DataFrame（索引=UTC+8 dt, 列=KLINE_COLUMNS）→ 结构化 BarArray。

    要求索引升序、无重复（normalizer 已保证）；dt 取毫秒整数（纪律 8.8）。
    """
    names = fields if fields is not None else BAR_NAMES
    arr = np.empty(len(df), dtype=BAR_DTYPE)
    arr["dt"] = dt_index_to_ms(df.index)
    for name in names[1:]:  # 跳过 dt
        if name in df.columns:
            arr[name] = df[name].to_numpy()
    return arr


def array_to_frame(arr: np.ndarray, fields: list[str] | None = None) -> pd.DataFrame:
    """BarArray 窗口 → DataFrame（索引=UTC+8 ms dt）。"""
    is_empty = arr.size == 0
    known = [f for f in BAR_NAMES if f != "dt"]
    cols = [f for f in known if fields is None or f in fields]
    if is_empty:
        return pd.DataFrame(columns=cols)
    data = {c: arr[c] for c in cols}
    # dt 为 UTC 绝对毫秒 → utc=True 再转上海墙钟（8.8 确定性; 不可 tz_localize, 会错位 8 小时）
    idx = pd.to_datetime(arr["dt"], unit="ms", utc=True).tz_convert("Asia/Shanghai")
    return pd.DataFrame(data, index=idx)
