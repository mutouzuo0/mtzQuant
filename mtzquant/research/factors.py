# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 23:50:00
# @update_time        : 2026/08/16 23:50:00
# @description : M3-S1 FactorEngine：因子注册表 + 全标的×全时间向量化计算（设计 5.8）

"""因子引擎（设计 5.8）——研究层普查：全标的×全时间矩阵, pandas 向量化。

因子注册表: `@register_factor(name, fields, source)` 声明式注册; 内置因子集:
  动量（N 日收益）/ 波动 / 相对量（换手代理）/ 市值 / PE / PB。

数据源双通道（共用同一 Provider, 5.8 训练所见=回测所见）:
  market      行情字段（close/volume…）→ provider.to_frame 批量宽表
  fundamental 财务字段（pe/pb/total_mv…）→ provider.fundamentals 逐标的 + **前向填充**
              （as-of 语义: 最新披露 ≤ t 的值在 t 可见——窗口只向左看, 防未来）。

输出: DataFrame(index=计算时点 as_of, columns=codes)——因子值在 t 只依赖 [.., t] 数据。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from mtzquant.core.errors import MtzQuantError


@dataclass(frozen=True)
class FactorSpec:
    """因子注册项: 依赖字段 + 数据源 + 计算函数。"""

    name: str
    fields: tuple[str, ...]
    source: str = "market"  # market | fundamental
    compute: Callable[[pd.DataFrame], pd.Series] | None = None
    description: str = ""


# 全局注册表（确定性: 名字唯一, 重复注册结构化报错）
FACTOR_REGISTRY: dict[str, FactorSpec] = {}


def register_factor(
    name: str, fields: tuple[str, ...] | list[str], *, source: str = "market", description: str = ""
) -> Callable[[Callable[[pd.DataFrame], pd.Series]], Callable[[pd.DataFrame], pd.Series]]:
    """注册因子（模块级装饰器; 名字重复抛结构化错误）。"""

    def deco(fn: Callable[[pd.DataFrame], pd.Series]) -> Callable[[pd.DataFrame], pd.Series]:
        if name in FACTOR_REGISTRY:
            raise MtzQuantError(
                f"因子 {name!r} 已注册", stage="factor", hint="因子注册表名字须唯一（5.8）"
            )
        FACTOR_REGISTRY[name] = FactorSpec(
            name=name, fields=tuple(fields), source=source, compute=fn, description=description
        )
        return fn

    return deco


def list_factors() -> list[str]:
    return sorted(FACTOR_REGISTRY)


# ============================================================
# 内置因子（向量化, 手算可对照 T-V01）
# ============================================================
def _momentum(sub: pd.DataFrame, n: int) -> pd.Series:
    return sub["close"].pct_change(n)


def _volatility(sub: pd.DataFrame, n: int) -> pd.Series:
    return sub["close"].pct_change().rolling(n).std()


def _relative_volume(sub: pd.DataFrame, n: int) -> pd.Series:
    short = sub["volume"].rolling(n).mean()
    long = sub["volume"].rolling(5 * n).mean()
    return short / long - 1.0


def _asof_field(sub: pd.DataFrame, col: str) -> pd.Series:
    """基本面字段: 已按 as-of 前向填充, 直接返回（t 处=最新披露值, 窗口只向左看）。"""
    return sub[col]


# 动量/波动/相对量参数化注册（momentum_20 / volatility_20 / relative_volume_20 等）
def _param_momentum(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    def _compute(sub: pd.DataFrame) -> pd.Series:
        return _momentum(sub, n)

    return _compute


def _param_volatility(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    def _compute(sub: pd.DataFrame) -> pd.Series:
        return _volatility(sub, n)

    return _compute


for _n in (5, 20, 60):
    register_factor(f"momentum_{_n}", ("close",), description=f"{_n} 日动量（收益率）")(
        _param_momentum(_n)
    )
    register_factor(f"volatility_{_n}", ("close",), description=f"{_n} 日收益波动率")(
        _param_volatility(_n)
    )
register_factor("relative_volume_20", ("volume",), description="20 日相对量（换手代理）")(
    lambda sub: _relative_volume(sub, 20)
)
register_factor("market_cap", ("total_mv",), source="fundamental", description="总市值（亿元）")(
    lambda sub: _asof_field(sub, "total_mv")
)
register_factor("pe", ("pe",), source="fundamental", description="市盈率（TTM, as-of）")(
    lambda sub: _asof_field(sub, "pe")
)
register_factor("pb", ("pb",), source="fundamental", description="市净率（as-of）")(
    lambda sub: _asof_field(sub, "pb")
)


def parse_factor_param(name: str) -> tuple[str, int | None]:
    """从因子名提取参数（momentum_20 → ('momentum', 20); 无参数 → (name, None)）。"""
    m = re.fullmatch(r"([a-z_]+?)_(\d+)", name)
    if m:
        return m.group(1), int(m.group(2))
    return name, None


def _sh(ts: datetime) -> pd.Timestamp:
    """归一为 Asia/Shanghai tz-aware 时间戳（已 aware 则 convert, 确定性 8.8）。"""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("Asia/Shanghai")
    return t.tz_convert("Asia/Shanghai")


class FactorEngine:
    """因子批量计算（设计 5.8）——全标的×全时间矩阵, 研究层普查入口。"""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    # ------------------------------------------------------------------
    def compute(
        self,
        name: str,
        codes: list[str],
        start: datetime,
        end: datetime,
        *,
        as_of_rule: str = "close",
        warmup: int | None = None,
    ) -> pd.DataFrame:
        """计算单因子全标的矩阵; 返回 DataFrame(index=计算时点, columns=codes)。

        as_of_rule: 'close'（收盘时点计算, 默认）| 'open'（预留: 用前一日数据）;
        因子值在 t 只依赖 ≤t 的可见数据（窗口只向左看, 防未来）。
        """
        if name not in FACTOR_REGISTRY:
            raise MtzQuantError(
                f"未知因子 {name!r}", stage="factor", hint=f"已注册: {list_factors()}"
            )
        spec = FACTOR_REGISTRY[name]
        if as_of_rule not in ("close", "open"):
            raise MtzQuantError(
                f"未知 as_of_rule {as_of_rule!r}", stage="factor", hint="close|open"
            )
        warm = warmup or self._default_warmup(spec)
        frame = self._as_of_frame(codes, spec.fields, spec.source, start, end, warm)
        out: dict[str, pd.Series] = {}
        for code in sorted(set(codes)):
            sub = frame[code].dropna(how="all")
            if sub.empty:
                continue
            s = spec.compute(sub) if spec.compute is not None else _asof_field(sub, spec.fields[0])
            out[code] = s
        if not out:
            raise MtzQuantError(
                f"因子 {name} 无任何标的可计算（数据不足）",
                stage="factor",
                hint="检查 codes/start/end 与数据覆盖（3.12）",
            )
        df = pd.DataFrame(out)
        # 裁剪到请求区间（计算起点前移 warmup, 输出只留 [start, end]）
        lo = df.index.searchsorted(_sh(start))
        df = df.iloc[lo:]
        return df.sort_index()

    @staticmethod
    def _default_warmup(spec: FactorSpec) -> int:
        """因子默认预热（动量/波动至少需要 2 倍窗口; 基本面字段无需历史回看）。"""
        if spec.source == "fundamental":
            return 5
        _, n = parse_factor_param(spec.name)
        return max(60, 2 * (n or 1) + 5)

    # ------------------------------------------------------------------
    # as-of 宽表（market + fundamental 双通道, 共用 Provider）
    # ------------------------------------------------------------------
    def _as_of_frame(
        self,
        codes: list[str],
        fields: tuple[str, ...],
        source: str,
        start: datetime,
        end: datetime,
        warmup: int,
    ) -> pd.DataFrame:
        if source == "market":
            wstart = _sh(start) - pd.Timedelta(days=int(warmup * 1.7) + 7)
            return self._provider.to_frame(list(codes), list(fields), wstart.to_pydatetime(), end)
        if source == "fundamental":
            return self._fundamental_frame(codes, list(fields), start, end, warmup)
        raise MtzQuantError(f"未知因子数据源 {source!r}", stage="factor", hint="market|fundamental")

    def _fundamental_frame(
        self,
        codes: list[str],
        fields: list[str],
        start: datetime,
        end: datetime,
        warmup: int,
    ) -> pd.DataFrame:
        """逐标的基本面 as-of 宽表（fundamentals ≤end 全量 → 对齐行情日期前向填充）。"""
        wstart = _sh(start) - pd.Timedelta(days=int(warmup * 1.7) + 7)
        frame = self._provider.to_frame(list(codes), ["close"], wstart.to_pydatetime(), end)
        if frame.empty:
            raise MtzQuantError("无行情日期轴（to_frame 空）", stage="factor")
        # 每个标的基本面行: ≤end 全量（as_of=end+1 天, 双时间校验只留已披露）,
        # 前向填充到行情日期轴 = as-of 语义（最新披露 ≤ t 的值在 t 可见, 防未来）
        for code in sorted(set(codes)):
            rows = self._provider.fundamentals(
                code, "daily_basic", fields, as_of=end + timedelta(days=1)
            )
            for fld in fields:
                if rows is None or rows.empty or fld not in rows.columns:
                    frame[(code, fld)] = pd.Series(index=frame.index, dtype=float)
                    continue
                frame[(code, fld)] = rows[fld].reindex(frame.index, method="ffill")
        return frame

    # ------------------------------------------------------------------
    def zscore(self, values: pd.DataFrame) -> pd.DataFrame:
        """横截面 Z 分（每期逐行标准化, 5.8.1 打分）——pandas 向量化。"""
        mean = values.mean(axis=1)
        std = values.std(axis=1)
        return values.sub(mean, axis=0).div(std.replace(0.0, float("nan")), axis=0)
