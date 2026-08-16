# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:22:00
# @update_time        : 2026/08/16 21:59:08
# @description : D3 DataNormalizer：三格式→内部统一 K 线（设计 3.3）

"""DataNormalizer（设计 3.3）——三段式管道的第 ② 段：把来源原始 CSV 洗成统一内部格式。

输入: SourceDriver.load_kline() 的输出（原始列名 + 已解析的 UTC+8 日期列）。
输出: 内部统一 K 线 DataFrame（全框架唯一认此格式）:
    index   = dt（UTC+8 毫秒时间戳; 日线=交易日 15:00 收盘时刻, 分钟=bar 结束时刻）
    columns = KLINE_COLUMNS（open/high/low/close/volume/amount/pre_close/
             suspended/limit_up/limit_down）

规则:
    时间戳归一   日线 → 当日 15:00（设计 3.3；从源头杜绝当日未来数据）
    复权处理    不在此层——raw 价唯一记账基准, 复权归指标层（设计 3.14）
    缺失规则    OHLC NaN → 沿用 pre_close; volume/amount NaN → 0; 全空行按停牌处理
    suspended   来源 paused 标记（聚宽）或 volume==0
    涨跌停价    由调用方注入的 limit_map 提供者计算（设计 5.4）——data 层禁止 import engine,
                品种档案从引擎侧传入, 本模块零业务规则硬编码

确定性: 输出按时间升序、重复 dt 保留最新一条（去重纪律 8.8/3.9）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import DAILY_BAR_TIME, KLINE_COLUMNS, Frequency
from mtzquant.data.drivers.csv_driver import CsvSourceDriver, _date_column

# ------------------------------------------------------------------
# 来源格式 → 规范列名映射（设计 3.5；键为列名小写）
# ------------------------------------------------------------------
_COLUMN_MAPS: dict[str, dict[str, str]] = {
    "tushare": {"vol": "volume", "amount": "amount"},
    "joinquant": {"volume": "volume", "money": "amount", "paused": "paused"},
    "generic": {"volume": "volume", "amount": "amount"},
}
# 三个格式共价列（open/high/low/close 原名列小写即规范名, 无需映射）
_OHLC_COLS = ("open", "high", "low", "close")


@runtime_checkable
class LimitMapProvider(Protocol):
    """涨跌停价计算提供者（设计 5.4 LimitRule.limit_map 的结构化协议）。

    卫星类型: 调用方注入（引擎侧 InstrumentProfile 天然满足），数据层不 import engine。
    """

    def limit_map(self, prev_close: float, *, is_st: bool = False) -> tuple[float, float]: ...


class DataNormalizer:
    """三格式原始宽表 → 内部统一 K 线（设计 3.3）。"""

    def __init__(self, frequency: Frequency = Frequency.D1) -> None:
        self.frequency = frequency

    def _bar_end(self, dt: pd.Timestamp) -> pd.Timestamp:
        """时间戳归一: 日线=当日 15:00（UTC+8 墙钟, tz-aware）; 分钟线=bar 结束时刻（M5）。

        统一约定: 归一输出索引为 Asia/Shanghai tz-aware——astype(int64)/timestamp() 得到
        UTC 绝对毫秒, 机器无关（确定性纪律 8.8）。
        """
        if self.frequency is Frequency.D1:
            wall = dt.tz_localize(None) if dt.tzinfo is not None else dt
            day = wall.floor("D")
            return (
                day + pd.Timedelta(hours=DAILY_BAR_TIME.hour, minutes=DAILY_BAR_TIME.minute)
            ).tz_localize("Asia/Shanghai")
        return dt

    def normalize(
        self,
        df: pd.DataFrame,
        code: str,
        *,
        fmt: str = "auto",
        limit: LimitMapProvider | None = None,
    ) -> pd.DataFrame:
        """把来源原始列洗成内部统一 K 线。

        参数:
            df     SourceDriver.load_kline() 的输出（含已解析的 UTC+8 日期列）
            code   标的（归一代码；仅用于报错信息）
            fmt    tushare | joinquant | generic | auto（auto 用列名嗅探）
            limit  涨跌停价提供者（InstrumentProfile；缺省则 limit_up/down 置 NaN）
        """
        if df is None or df.empty:
            raise MtzQuantError(
                f"归一输入为空: {code}", stage="normalizer", hint="检查 K 线 CSV 数据行与区间裁剪"
            )

        lower = {c.lower(): c for c in df.columns}
        if fmt == "auto":
            fmt = CsvSourceDriver.sniff_format(list(lower))
        if fmt not in _COLUMN_MAPS:
            raise MtzQuantError(
                f"归一不支持的格式 {fmt!r}",
                stage="normalizer",
                hint="driver 已保证格式 ∈ {auto,tushare,joinquant,generic}",
            )

        # 1) 列名映射到规范名（含 OHLC 原列名大小写归一）
        rename = {lower[k]: v for k, v in _COLUMN_MAPS[fmt].items() if k in lower}
        for canonical in _OHLC_COLS:
            actual = lower.get(canonical)
            if actual:
                rename[actual] = canonical
        work = df.rename(columns=rename)

        # 2) 时间索引（driver 已解析 UTC+8 日期列）
        date_col = _date_column(fmt, list(df.columns))
        if date_col not in df.columns:
            raise MtzQuantError(
                f"格式 {fmt} 缺少已解析日期列 {date_col!r}",
                stage="normalizer",
                hint="driver 解析日期列后返回；缺失说明上层调用未走 driver.load_kline",
            )
        dt_series = pd.to_datetime(df[date_col])

        # 3) 契约列装配（按位置取值——dict of Series + index 会按标签对齐导致 NaN, 故先转 ndarray）
        out = pd.DataFrame(
            {
                c: (work[c].to_numpy(dtype="float64") if c in work.columns else np.nan)
                for c in KLINE_COLUMNS
            },
            index=dt_series,
        )
        # paused 作为临时列随行保留（参与后续去重/排序, 再移除）
        src_paused = work.get("paused")
        if src_paused is not None:
            out["_paused"] = pd.to_numeric(src_paused, errors="coerce").fillna(0).to_numpy() > 0
        out = out[~out.index.isna()]

        # 4) 时间戳归一 + 排序去重（保留最新, 纪律 3.9/8.8）
        out.index = out.index.map(self._bar_end)
        out = out[~out.index.duplicated(keep="last")].sort_index()

        # 5) pre_close（昨收=前一根收盘; 首根 NaN 由上层按需处理）
        out["pre_close"] = out["close"].shift(1)

        # 6) suspended（来源 paused 标记或 volume==0, 设计 3.3）
        paused = (
            out.pop("_paused") if "_paused" in out.columns else pd.Series(False, index=out.index)
        )
        out["suspended"] = (paused | (out["volume"].replace(np.nan, 0.0) <= 0)).astype("int64")

        # 6b) 缺失值规则: OHLC NaN → 前向沿用; 停牌日(量零/paused) 价格为 0 或 NaN → 用前收
        susp_mask = out["suspended"].astype(bool).to_numpy()
        for c in _OHLC_COLS:
            vals = out[c].to_numpy(dtype="float64")
            filled = pd.Series(vals).ffill().to_numpy(dtype="float64").copy()
            use_pre = susp_mask & (np.isnan(filled) | (filled == 0))
            if use_pre.any():
                filled[use_pre] = out["pre_close"].to_numpy(dtype="float64")[use_pre]
            out[c] = filled
        out["volume"] = out["volume"].replace(np.nan, 0.0)
        out["amount"] = out["amount"].replace(np.nan, 0.0)
        out["pre_close"] = out["pre_close"].replace(np.nan, np.nan)  # 首行保持 NaN（语义: 无昨收）

        # 7) 涨跌停价（由注入档案计算; 无档案 → NaN 列）
        if limit is not None:
            up, down = self._compute_limits(out["pre_close"], limit)
            out["limit_up"], out["limit_down"] = up, down
        else:
            out["limit_up"], out["limit_down"] = np.nan, np.nan

        return out.loc[:, list(KLINE_COLUMNS)]

    @staticmethod
    def _compute_limits(
        pre_close: pd.Series, limit: LimitMapProvider
    ) -> tuple[pd.Series, pd.Series]:
        up = pd.Series(index=pre_close.index, dtype="float64")
        dn = pd.Series(index=pre_close.index, dtype="float64")
        for idx, pc in pre_close.items():
            if pd.isna(pc) or pc <= 0:
                up[idx], dn[idx] = np.nan, np.nan
                continue
            lo, hi = limit.limit_map(float(pc))
            dn[idx], up[idx] = lo, hi
        return up, dn


def dt_index_to_ms(index: pd.DatetimeIndex) -> np.ndarray:
    """DatetimeIndex → UTC+8 毫秒整数数组（确定性 8.8; 显式 ns 基准, 防 pandas 3 us 单位坑）。"""
    return index.as_unit("ns").asi8 // 1_000_000


def to_ms_index(df: pd.DataFrame) -> np.ndarray:
    """返回索引（UTC+8 时间戳）的整数毫秒数组——确定性/机读导出统一用（设计 8.8）。"""
    return dt_index_to_ms(df.index)
