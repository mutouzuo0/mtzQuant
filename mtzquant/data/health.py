# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 03:00:00
# @update_time        : 2026/08/17 03:00:00
# @description : M4-Z1 数据体检：DuckDB 全库扫描（覆盖/缺失/0价0量/OHLC越界/重复dt, 3.10/7.7）

"""数据体检（设计 3.10/7.7, M4-Z1）——全库 K 线质量扫描。

对 kline/{type}/day/*.csv 逐标的跑:
  覆盖摘要（min/max/count, 3.9-①）+ DuckDB 质量模板（QUALITY_CHECKS, 3.10）:
  缺失交易日（周一~五近似）/ 0 价 0 量 / OHLC 越界 / 重复 dt / 日期解析失败。

产物: HealthReport（JSON/机读）——`mtzquant health` CLI 与 `GET /api/health` 双面共用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtzquant.core.errors import MtzQuantError
from mtzquant.data.duckdb_query import DuckDBQuery


@dataclass
class InstrumentHealth:
    """单标的体检摘要。"""

    code: str
    count: int = 0
    min_date: str = ""
    max_date: str = ""
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)  # 质量模板 → 违规数+样例

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "count": self.count,
            "range": [self.min_date, self.max_date],
            "issues": self.checks,
        }


@dataclass
class HealthReport:
    """全库体检报告。"""

    instruments: list[InstrumentHealth] = field(default_factory=list)
    scanned_at: str = ""

    @property
    def total(self) -> int:
        return len(self.instruments)

    @property
    def issue_count(self) -> int:
        return sum(len(h.checks) for h in self.instruments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_at": self.scanned_at,
            "instruments": self.total,
            "issue_count": self.issue_count,
            "items": [h.to_dict() for h in self.instruments],
        }


def scan_health(data_root: Path | str) -> HealthReport:
    """扫描 kline/{etf,stock}/day/*.csv 全库体检（3.10/7.7）。"""
    root = Path(data_root)
    kline = root / "kline"
    report = HealthReport(
        scanned_at=__import__("datetime").datetime.now().isoformat(timespec="seconds")
    )
    if not kline.is_dir():
        return report
    files: list[Path] = []
    for t in ("etf", "stock", "index"):
        d = kline / t / "day"
        if d.is_dir():
            files += sorted(d.glob("*.csv"))
    q = DuckDBQuery()
    try:
        for f in files:
            try:
                report.instruments.append(_scan_one(q, f))
            except MtzQuantError:
                continue  # 单文件读取失败跳过（其余继续）
    finally:
        q.close()
    return report


def _scan_one(q: DuckDBQuery, path: Path) -> InstrumentHealth:
    """单标的: 覆盖 + DuckDB 质量模板（违规记数 + 前 3 样例）。"""
    raw = q.read_kline(path)
    if raw is None or raw.empty:
        return InstrumentHealth(code=path.stem, checks={"empty_file": {"n": 1}})
    dts = (
        __import__("pandas")
        .to_datetime(raw["trade_date"], format="%Y%m%d", errors="coerce")
        .dropna()
    )
    health = InstrumentHealth(
        code=path.stem,
        count=len(dts),
        min_date=str(dts.min().date()) if len(dts) else "",
        max_date=str(dts.max().date()) if len(dts) else "",
    )
    checks = q.quality_report(path)
    for name, df in checks.items():
        if df is None:
            health.checks[name] = {"error": "query_failed"}
        elif df.empty:
            continue
        else:
            health.checks[name] = {
                "n": int(len(df)),
                "sample": df.head(3).to_dict(orient="records"),
            }
    return health
