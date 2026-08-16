# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:15:00
# @update_time        : 2026/08/16 21:59:08
# @description : D2 CsvSourceDriver：三格式嗅探/路径模板/平铺挂载（设计 3.5/3.12）

"""CSV 数据源驱动（设计 3.5 / 3.12）——v1 唯一实现。

职责边界: 只做「把 CSV 读出来 + 按区间裁剪」, 列仍保持来源原始命名
（列名映射/代码归一/时间戳 15:00 化由 DataNormalizer 处理）。

特性:
  三格式嗅探（读头部前 5 行判定）—— tushare / joinquant(聚宽) / generic(通用);
    显式 format 配置跳过嗅探; 未知格式报错并列出期望列名（AI 友好）。
  路径模板 —— kline_day_dir="kline/{type}/day" + file_pattern="{code}.csv";
    group_by_type=false 时 {type} 占位被剥离, 退化为平铺目录（兼容 khQuant 等旧布局, 设计 3.12）。
  编码可配（默认 utf-8-sig）; 日期解析: tushare trade_date 为 YYYYMMDD, 其余按列推断。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from mtzquant.config import LocalCsvSettings
from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import AdjustMode, Frequency, InstrumentType
from mtzquant.data.drivers.base import InstrumentRef

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _as_shanghai(dt: datetime) -> datetime:
    """把可能 naive 的边界时间解释为上海墙钟（tz-aware 原样保留）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_SHANGHAI)
    return dt


# 三种格式的期望列名全集（未知格式报错时列出，AI/人工对照用）
_EXPECTED_COLUMNS: dict[str, list[str]] = {
    "tushare": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
    "joinquant": ["time", "open", "high", "low", "close", "volume", "money", "paused", "factor"],
    "generic": ["date", "time", "open", "high", "low", "close", "volume", "amount"],
}

# 品种探测顺序（K 线文件不带 master 主数据时, 按此顺序找一个存在的路径；约定 3.12）
_SEARCH_TYPES: tuple[InstrumentType, ...] = (
    InstrumentType.ETF,
    InstrumentType.STOCK,
    InstrumentType.INDEX,
)


def _date_column(fmt: str, columns: list[str]) -> str:
    """该格式应使用的日期列列名（generic 的 date/time 二选一, 以实际列为准）。"""
    low = {c.lower(): c for c in columns}
    if fmt == "tushare":
        return low.get("trade_date", "trade_date")
    if fmt == "joinquant":
        return low.get("time", "time")
    return low["date"] if "date" in low else low["time"]


def _parse_date_series(raw: pd.Series, fmt: str) -> pd.Series:
    """把日期列解析为 UTC+8 的 tz-aware datetime（未指定时间的行视为当日 00:00）。

    日线 15:00 收盘时刻的偏移由 DataNormalizer 统一完成（设计 3.3）——驱动不越权。
    """
    if fmt == "tushare":
        text = raw.astype("str").str.strip()
        parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    else:
        parsed = pd.to_datetime(raw, errors="coerce")
    bad = int(parsed.isna().sum())
    if bad:
        raise MtzQuantError(
            f"CSV 日期列解析失败 {bad} 行（格式 {fmt}）",
            stage="csv_driver",
            hint="检查日期列内容是否符合格式规范（tushare=YYYYMMDD, 其余自动推断）",
        )
    if parsed.dt.tz is None:
        parsed = parsed.dt.tz_localize("Asia/Shanghai")
    else:
        parsed = parsed.dt.tz_convert("Asia/Shanghai")
    return parsed


class CsvSourceDriver:
    """本地 CSV 行情驱动（设计 3.5/3.12）。"""

    name: str = "local_csv"

    def __init__(self, settings: LocalCsvSettings | None = None, **kwargs: Any) -> None:
        # 兼容两种装配: create_driver(settings=...) 或 create_driver(root_path=..., format=...)
        if settings is None:
            settings = LocalCsvSettings(**kwargs)
        self.settings = settings
        self._root = Path(settings.root_path)

    # ------------------------------------------------------------------
    # 路径解析（设计 3.12：品种分组可关、文件名为归一代码）
    # ------------------------------------------------------------------
    def kline_dir(self, instrument_type: InstrumentType, frequency: Frequency) -> Path:
        """解析 K 线目录（含 {type} 占位）。

        group_by_type=true:  {type} → 品种值（kline/{type}/day → kline/etf/day）
        group_by_type=false: {type} 占位剥离（kline/{type}/day → kline/day，兼容旧平铺布局）
        """
        template = (
            self.settings.kline_day_dir
            if frequency is Frequency.D1
            else self.settings.kline_minute_dir
        )
        if self.settings.group_by_type:
            rendered = template.replace("{type}", instrument_type.value)
        else:
            rendered = template.replace("/{type}/", "/").replace("{type}", "")
        return self._root / rendered

    def kline_path(self, code: str, frequency: Frequency) -> Path:
        """定位单标的 K 线文件（跨品种探测：master 缺档时按 ETF→股票→指数顺序）。"""
        file_name = self.settings.file_pattern.replace("{code}", code)
        trials = [self.kline_dir(t, frequency) / file_name for t in _SEARCH_TYPES]
        for p in trials:
            if p.exists():
                return p
        raise MtzQuantError(
            f"K 线文件不存在: {code}（{frequency.value}，尝试目录: {trials[0].parent}）",
            stage="csv_driver",
            hint=(
                "检查 3.12 目录布局与 settings.local_csv（root_path/kline_day_dir/group_by_type）；"
                "或先运行 fetch-etf 下载数据"
            ),
        )

    # ------------------------------------------------------------------
    # 格式识别（设计 3.5：读头部 5 行判定；显式 format 跳过嗅探）
    # ------------------------------------------------------------------
    def _read_header(self, path: Path) -> list[str]:
        try:
            text = path.read_text(encoding=self.settings.encoding)
        except UnicodeDecodeError as exc:  # noqa: BLE001
            raise MtzQuantError(
                f"CSV 编码解析失败（按 {self.settings.encoding} 读取）: {path}",
                stage="csv_driver",
                hint="settings.local_csv.encoding 可配为 gbk / utf-8 / utf-8-sig（设计 3.5）",
            ) from exc
        except OSError as exc:
            raise MtzQuantError(
                f"无法读取 CSV 文件: {path}",
                stage="csv_driver",
                hint=f"原因为 {exc}; 检查数据目录与 3.12 布局",
            ) from exc
        for line in text.splitlines()[:5]:
            cleaned = line.strip().lstrip("\ufeff")
            if cleaned:
                return [col.strip() for col in cleaned.split(",")]
        return []

    @staticmethod
    def sniff_format(columns: list[str]) -> str:
        """由列名判定格式（tushare / joinquant / generic）；无法识别抛结构化错误。"""
        cols = {c.strip().lower() for c in columns if c.strip()}
        if "ts_code" in cols and "trade_date" in cols:
            return "tushare"
        if "time" in cols and "paused" in cols:
            return "joinquant"
        if "date" in cols or "time" in cols:
            return "generic"
        expected = ", ".join(
            sorted(
                set(
                    _EXPECTED_COLUMNS["tushare"]
                    + _EXPECTED_COLUMNS["joinquant"]
                    + _EXPECTED_COLUMNS["generic"]
                )
            )
        )
        raise MtzQuantError(
            f"无法识别的 CSV 格式（列: {sorted(cols)}）",
            stage="csv_driver",
            hint=(
                f"期望列名（三格式并集）: {expected}；"
                "或显式配置 format='tushare'|'joinquant'|'generic' 跳过嗅探（设计 3.5）"
            ),
        )

    def _resolve_format(self, columns: list[str]) -> str:
        fmt = self.settings.format
        if fmt == "auto":
            return self.sniff_format(columns)
        if fmt not in _EXPECTED_COLUMNS:
            raise MtzQuantError(
                f"未知显式 format: {fmt!r}",
                stage="csv_driver",
                hint="format 可为 auto / tushare / joinquant / generic（设计 3.5）",
            )
        return fmt

    # ------------------------------------------------------------------
    # SourceDriver 协议
    # ------------------------------------------------------------------
    def list_instruments(
        self, instrument_type: InstrumentType | None = None
    ) -> list[InstrumentRef]:
        """列举本驱动可提供的标的（扫描 K 线目录下 {code}.csv）。"""
        types = (
            [instrument_type]
            if instrument_type is not None
            else [InstrumentType.STOCK, InstrumentType.ETF, InstrumentType.INDEX]
        )
        refs: list[InstrumentRef] = []
        pattern = self.settings.file_pattern
        if pattern.count("{code}") != 1:
            raise MtzQuantError(
                f"file_pattern 必须恰好含一个 {{code}} 占位，得到 {pattern!r}",
                stage="csv_driver",
            )
        prefix, suffix = pattern.split("{code}")
        for t in types:
            t_dir = self.kline_dir(t, Frequency.D1)
            if not t_dir.is_dir():
                continue
            for f in sorted(t_dir.glob("*.csv")):
                if not f.name.endswith(suffix):
                    continue
                if prefix and not f.name.startswith(prefix):
                    continue
                code = f.stem
                refs.append(
                    InstrumentRef(
                        code=code,
                        instrument_type=t,
                        exchange=code.rsplit(".", 1)[-1] if "." in code else "",
                    )
                )
        return refs

    def load_kline(
        self,
        code: str,
        frequency: Frequency,
        start: datetime,
        end: datetime,
        fields: list[str] | None = None,
        adjusted: AdjustMode = AdjustMode.NONE,
    ) -> pd.DataFrame:
        """读取单标的 K 线并按 [start, end] 裁剪；返回原始列（交给 DataNormalizer）。"""
        if frequency is not Frequency.D1:
            raise MtzQuantError(
                f"v1 仅支持日线 CSV，频率 {frequency.value!r} 未实现",
                stage="csv_driver",
                hint="分钟级数据 M5 落地（parquet 分片，设计 3.7/3.12）；接口已在协议中让位",
            )
        if adjusted is not AdjustMode.NONE:
            raise MtzQuantError(
                f"CSV 组件复权未实现（adjusted={adjusted.value}）",
                stage="csv_driver",
                hint="复权因子在 factor/adj_factor 目录，M2 DataFetcher 接入（设计 3.14）",
            )
        norm = normalize_code(code)
        path = self.kline_path(norm, frequency)
        columns = self._read_header(path)
        fmt = self._resolve_format(columns)
        df = pd.read_csv(path, low_memory=False)
        dt_col = _date_column(fmt, list(df.columns))
        if dt_col not in df.columns:
            raise MtzQuantError(
                f"格式判定为 {fmt}，但缺少日期列 {dt_col!r}（实际列: {list(df.columns)[:12]}...）",
                stage="csv_driver",
                hint="嗅探与后续列名推断必须一致；检查文件头列名",
            )
        df[dt_col] = _parse_date_series(df[dt_col], fmt)
        start = _as_shanghai(start)
        end = _as_shanghai(end)
        mask = (df[dt_col] >= start) & (df[dt_col] <= end)
        df = df.loc[mask].copy()
        if fields is not None:
            keep = [c for c in fields if c in df.columns]
            df = df[keep]
        return df

    # ---- 协议预留（设计 3.2: 接口让位, M5 实现）----
    def load_tick(self, code: str, start: datetime, end: datetime) -> pd.DataFrame:
        return self._not_implemented("load_tick")

    def load_trades(self, code: str, start: datetime, end: datetime) -> pd.DataFrame:
        return self._not_implemented("load_trades")

    def load_orders(self, code: str, start: datetime, end: datetime) -> pd.DataFrame:
        return self._not_implemented("load_orders")

    def load_depth(self, code: str, start: datetime, end: datetime) -> pd.DataFrame:
        return self._not_implemented("load_depth")

    @staticmethod
    def _not_implemented(api: str) -> pd.DataFrame:
        raise MtzQuantError(
            f"CSV 驱动 {api} 未实现（M5）",
            stage="csv_driver",
            hint="Tick/逐笔委托/盘口由 M5 分钟级数据与 ParquetDriver 承接（设计 3.2/3.7）",
        )
