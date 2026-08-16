# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:55:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-I02 真实 ETF 数据端到端冒烟（@slow @network, 缺数据 skip）+ 演示任务配置校验

"""T-I02（设计 12.1-M1 真实数据验收, 依赖阶段 E 下载器落盘）。

数据缺失时 skip 并提示先 `mtzquant fetch-etf --demo`; 数据在时:
  `mtzquant run`（demo_dual_ma 双均线 510300.SH）→ nav 有限非 NaN / 成交>0 /
  metrics 完整 / 报告生成。

配套非 slow 用例: 演示任务 JSON 配置合法 + 策略可加载（无需真实数据）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from mtzquant.config import (
    CacheSettings,
    DatabaseSettings,
    DataSettings,
    LocalCsvSettings,
    Settings,
)
from mtzquant.engine.export import RunStore
from mtzquant.engine.runner import run_task
from mtzquant.engine.session import TaskConfig
from tests.fixtures.synth import write_etf_csv

DEMO_TASK = Path("configs/demo_dual_ma.json")
DEMO_STRATEGY = Path("strategies/native/dual_ma.py")


def test_demo_task_config_and_strategy_valid() -> None:
    """演示任务（configs/demo_dual_ma.json + strategies/native/dual_ma.py）配置合法、可加载。"""
    task = TaskConfig.model_validate(json.loads(DEMO_TASK.read_text(encoding="utf-8")))
    assert task.task_name == "demo_dual_ma"
    assert task.universe == ["510300.SH"]
    assert task.backtest.start < task.backtest.end
    assert DEMO_STRATEGY.is_file()
    # 策略含 native 入口（AdapterRegistry.detect, 4.3）
    from mtzquant.adapters.base import AdapterRegistry

    code = DEMO_STRATEGY.read_text(encoding="utf-8")
    assert AdapterRegistry().detect(code) == "native"


def test_demo_dual_ma_strategy_end_to_end(tmp_path: Path) -> None:
    """演示策略（dual_ma.py）用合成趋势数据端到端跑通: 成交>0 + nav 有限 + 报告生成。

    验证「演示任务」生产路径（真实 demo 策略 + 生产会话, 4.9.2 只做六要素口径不做收益评判）。
    """
    data_root = tmp_path / "data"
    # 上升趋势序列 → 金叉买入（fast>slow）, 至少 1 笔成交
    write_etf_csv(data_root, "510300.SH", n=250, start="2020-01-02", drift=0.001, volatility=0.01)
    demo = TaskConfig.model_validate(json.loads(DEMO_TASK.read_text(encoding="utf-8")))
    # 覆盖: 数据目录/区间/策略绝对路径（保持 demo 双均线逻辑）; 整体重建以触发 pydantic 校验
    task = TaskConfig.model_validate(
        {
            **demo.model_dump(),
            "strategy": {**demo.strategy.model_dump(), "file": str(DEMO_STRATEGY.resolve())},
            "backtest": {
                **demo.backtest.model_dump(),
                "start": "2020-01-02",
                "end": "2020-12-31",
            },
        }
    )
    settings = Settings(
        data=DataSettings(
            local_csv=LocalCsvSettings(root_path=str(data_root)),
            cache=CacheSettings(enabled=True, parquet_dir=str(tmp_path / ".cache" / "parquet")),
        ),
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'zq.db'}"),
    )
    result = run_task(task, settings=settings, out_root=tmp_path / "results")
    navs = result.bundle.navs
    assert len(result.bundle.fills) >= 1, "双均线金叉应产生买入成交"
    assert navs and all(np.isfinite(r["nav"]) for r in navs)
    # 报告生成（9.2: run 导出 → report 渲染）
    from mtzquant.engine.report import render_report

    report_path = render_report(result.run_id, out_root=tmp_path / "results")
    assert report_path.is_file()


@pytest.mark.slow
@pytest.mark.network
def test_ti02_real_etf_smoke_demo_dual_ma(tmp_path: Path) -> None:
    """真实 ETF 数据端到端: 双均线任务 run → 指标 → report（缺数据 skip 提示先 fetch-etf）。"""
    kline = Path("data") / "kline" / "etf" / "day" / "510300.SH.csv"
    if not kline.is_file():
        pytest.skip("真实 ETF 数据缺失: 先运行 `mtzquant fetch-etf --demo`（阶段 E 下载器, 3.9）")

    task = TaskConfig.model_validate(json.loads(DEMO_TASK.read_text(encoding="utf-8")))
    settings = Settings()  # 默认 root_path=./data（仓库根, 与 fetch-etf 落盘一致）
    result = run_task(task, settings=settings, out_root=tmp_path / "results")

    navs = result.bundle.navs
    assert navs, "净值序列为空（真实数据未读入）"
    assert all(np.isfinite(r["nav"]) for r in navs), "净值含 NaN/inf（数据/撮合异常）"
    assert len(result.bundle.fills) > 0, "双均线策略应产生成交"
    # metrics 完整且有限
    summary = RunStore().compute_summary(result.bundle)
    m = summary.get("metrics", {})
    assert "total_return" in m and "sharpe" in m
    assert np.isfinite(m["total_return"])
    # 报告生成（9.2 自包含; run 导出 → report 渲染）
    from mtzquant.engine.report import render_report

    report_path = render_report(result.run_id, out_root=tmp_path / "results")
    assert report_path.is_file()


@pytest.mark.slow
@pytest.mark.network
def test_o_fetcher_real_etf_incremental(tmp_path: Path) -> None:
    """O 验收（3.9）: 真实拉取 3 只 ETF 增量补洞 + 幂等（网络不可用 skip）。"""
    from datetime import date, timedelta

    from mtzquant.data.fetcher import DataFetcher
    from mtzquant.data.ratelimit import RetryPolicy

    root = tmp_path / "data"
    fetcher = DataFetcher(
        root,
        sources=["akshare", "tushare"],
        checkpoint_dir=tmp_path / ".cache" / "checkpoint",
        cache_dir=tmp_path / ".cache" / "parquet",
        retry_policy=RetryPolicy(max_retries=0),  # 无网络环境快速失败（单次尝试）
    )
    end = date.today()
    start = end - timedelta(days=15)
    reports = fetcher.fetch(["510300.SH", "510500.SH", "159915.SZ"], start, end)

    ok = [r for r in reports if r.status == "ok"]
    if not ok:
        pytest.skip(f"网络不可用或源失败: {[r.reason for r in reports][:2]}")
    # 落盘 + 幂等: 同参数重复 fetch → skipped（零下载零写入, 3.9）
    for r in ok:
        assert (root / "kline" / "etf" / "day" / f"{r.code}.csv").is_file()
    again = fetcher.fetch(["510300.SH"], start, end)
    assert again[0].status == "skipped", "重复 fetch 应零下载零写入（幂等）"
