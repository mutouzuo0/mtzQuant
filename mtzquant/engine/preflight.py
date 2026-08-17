# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 10:00:00
# @update_time        : 2026/08/17 10:00:00
# @description : backtest 命令预检：策略标检测 / 数据完整性 / 缺失下载 / Web 地址（M4 后置增强）

"""`mtzquant backtest` 一键回测的预检编排（计划: 检测标的→校验数据→缺则下载→回测→Web 地址）。

- `detect_codes(source)`: 从策略源码抽候选证券代码（跳过注释行、排除 set_benchmark 基准）,
  逐个 `normalize_code` 校验归一（InvalidCodeError 过滤非代码）——best-effort, 任务 universe 恒兜底;
- `required_codes(task)`: task.universe ∪ 策略检测 ∪ benchmark → 归一排序;
- `check_data` / `ensure_data`: CoverageChecker.gaps 判缺失, DataFetcher.fetch 只下缺失段
  （内部已含 gaps 裁剪; fetch_fn 供测试注入）;
- `web_url(run_id, settings)`: `http://{server.host}:{server.port}/#/monitor?run=<run_id>`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from mtzquant.config import Settings
from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError
from mtzquant.data.fetcher import DataFetcher

# 候选代码串正则——**必须带交易所标记**（后缀 .SH/.SZ/.XSHG/.SS 或前缀 sh/sz/bj）,
# 裸 6 位数字（如 order_target_value(code, 500000) 的金额字面量）不当作代码, 防误检。
_CODE_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z])((?:sh|sz|bj)\d{6}|\d{6}\.(?:XSHG|XSHE|SS|SH|SZ|BJ))(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_QMT_RE = re.compile(r"(?<![0-9A-Za-z])([01])\.(\d{6})(?![0-9A-Za-z])")


@dataclass
class PreflightReport:
    """标的数据完整性预检报告。"""

    codes: list[str] = field(default_factory=list)  # 全部需数据标的（归一）
    ok: list[str] = field(default_factory=list)  # 区间完整
    missing: dict[str, list[tuple[date, date]]] = field(default_factory=dict)  # code → 缺失段
    downloaded: list[str] = field(default_factory=list)  # 本次补下载的标的
    check_only: bool = False

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "codes": self.codes,
            "complete": self.complete,
            "missing": {
                c: [[s.isoformat(), e.isoformat()] for s, e in segs]
                for c, segs in self.missing.items()
            },
            "downloaded": self.downloaded,
            "check_only": self.check_only,
        }


# ------------------------------------------------------------------
# 标的检测（best-effort; 任务 universe 恒兜底）
# ------------------------------------------------------------------
def detect_codes(strategy_source: str) -> list[str]:
    """从策略源码提取涉及的证券代码（归一内码, 排序去重）。

    规则: 去掉行内/整行 `#` 注释（全天候含 `{ #'159915.XSHE'...}` 之类行内注释陷阱）;
    排除 `set_benchmark(...)` 行（基准非交易标的, benchmark 由 required_codes 单独并入）;
    候选串经 normalize_code 校验。
    """
    found: set[str] = set()
    for line in strategy_source.splitlines():
        code_part = line.split("#", 1)[0].strip()  # 剥注释（整行注释 → 空）
        if not code_part:
            continue
        if "set_benchmark" in code_part:
            continue
        for m in _CODE_CANDIDATE_RE.finditer(code_part):
            try:
                found.add(normalize_code(m.group(1)))
            except MtzQuantError:
                pass
        for m in _QMT_RE.finditer(code_part):
            try:
                found.add(normalize_code(f"{m.group(1)}.{m.group(2)}"))
            except MtzQuantError:
                pass
    return sorted(found)


def required_codes(task: Any, strategy_source: str | None = None) -> list[str]:
    """回测所需全部标的 = 任务 universe ∪ 策略检测 ∪ 基准（归一排序）。"""
    codes: set[str] = set(normalize_code(c) for c in task.universe)
    if strategy_source:
        codes.update(detect_codes(strategy_source))
    bench = getattr(task.backtest, "benchmark", None)
    if bench:
        try:
            codes.add(normalize_code(bench))
        except MtzQuantError:
            pass
    return sorted(codes)


# ------------------------------------------------------------------
# 数据完整性检测 + 缺失下载（DataFetcher 复用, 只下缺失段）
# ------------------------------------------------------------------
def check_data(settings: Settings, codes: list[str], start: date, end: date) -> PreflightReport:
    """对每个标的查回测区间缺失段（CoverageChecker.gaps, 3.9-①）。"""
    fetcher = DataFetcher(Path(settings.data.local_csv.root_path))
    report = PreflightReport(codes=[normalize_code(c) for c in codes])
    for code in report.codes:
        _typ, gaps = fetcher.gaps(code, start, end)
        if gaps:
            report.missing[code] = list(gaps)
        else:
            report.ok.append(code)
    return report


def ensure_data(
    settings: Settings,
    codes: list[str],
    start: date,
    end: date,
    *,
    auto_fetch: bool = True,
    fetch_fn: Any | None = None,
) -> PreflightReport:
    """预检; 缺失且 auto_fetch → 下载缺失段后复检（fetch 内部只下缺失段）。

    fetch_fn 供测试注入（绕过网络）: `(code, start, end, *, source, instrument_type) -> DataFrame`。
    """
    fetcher = DataFetcher(Path(settings.data.local_csv.root_path), fetch_fn=fetch_fn)
    report = PreflightReport(codes=[normalize_code(c) for c in codes], check_only=not auto_fetch)
    missing: dict[str, list[tuple[date, date]]] = {}
    for code in report.codes:
        _typ, gaps = fetcher.gaps(code, start, end)
        if gaps:
            missing[code] = list(gaps)
        else:
            report.ok.append(code)
    report.missing = missing
    if missing and auto_fetch:
        # DataFetcher.fetch 内部对每个 code 先 gaps 再只下载缺失段（3.9 六步管道）
        reports = fetcher.fetch(sorted(missing), start, end)
        report.downloaded = [r.code for r in reports if r.status in ("ok", "skipped")]
        # 复检（幂等; 仍缺则记入 missing 由上层提示）
        report.missing = {}
        report.ok = []
        for code in report.codes:
            _typ, gaps = fetcher.gaps(code, start, end)
            if gaps:
                report.missing[code] = list(gaps)
            else:
                report.ok.append(code)
    return report


# ------------------------------------------------------------------
# Web 地址（ServerSettings.host/port; 默认 127.0.0.1:8501）
# ------------------------------------------------------------------
def web_url(run_id: str, settings: Settings) -> str:
    host = settings.server.host or "127.0.0.1"
    port = settings.server.port or 8501
    return f"http://{host}:{port}/#/monitor?run={run_id}"
