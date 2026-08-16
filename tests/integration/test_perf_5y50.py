# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:50:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-P01 性能预算（50 标的×5 年 <30s）+ T-P02 写库不阻塞（引擎占比>90%）

"""T-P01/T-P02（设计 12.1-M1 性能验收, 普通笔记本口径）。

T-P01: 合成 50 标的 × 5 年日线（含预热）全链路（加载+回测+入库）<30s;
       二次运行数据准备（L2 parquet 命中）<2s; 打印分解（加载/引擎/导出/写库）。
T-P02: 同场景下引擎纯计算占比 >90%（写库不阻塞主循环, 8.7 异步化验证）。

合成数据向量化生成（numpy, 确定性 seed=42）; 策略=首日等权建仓持有（50 单 50 成交）。
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mtzquant.config import (
    CacheSettings,
    DatabaseSettings,
    DataSettings,
    LocalCsvSettings,
    Settings,
)
from mtzquant.engine.runner import build_pipeline, run_task
from mtzquant.engine.session import TaskConfig

N_CODES = 50
N_DAYS = 5 * 250  # 5 年 × 250 交易日
START = "2020-01-02"


def _trade_days(start: date, n: int) -> list[str]:
    days: list[str] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return days


def _gen_etf_pool(data_root: Path, *, n: int = N_CODES) -> list[str]:
    """向量化生成 n 个 ETF 日线 CSV（tushare 源格式, 确定性 8.8）。"""
    rng = np.random.default_rng(42)
    days = _trade_days(date.fromisoformat(START), N_DAYS)
    codes = [f"510{i:03d}.SH" for i in range(n)]  # 510000..510049
    log_ret = rng.normal(0.0002, 0.01, size=(len(days), n))
    price = 10.0 * np.exp(np.cumsum(log_ret, axis=0))
    for j, code in enumerate(codes):
        closes = price[:, j]
        opens = closes * (1 + rng.normal(0, 0.002, size=len(days)))
        highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.002, len(days))))
        lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.002, len(days))))
        vol = np.full(len(days), 5_000_000, dtype=float)
        df = pd.DataFrame(
            {
                "ts_code": code,
                "trade_date": days,
                "open": opens.round(4),
                "high": highs.round(4),
                "low": lows.round(4),
                "close": closes.round(4),
                "vol": vol.astype(int),
                "amount": (vol * closes).round(2),
            }
        )
        path = data_root / "kline" / "etf" / "day" / f"{code}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8")
    return codes


def _buy_hold_strategy(codes: list[str]) -> str:
    code_list = json.dumps(codes, ensure_ascii=False)
    return (
        "def initialize(context):\n"
        f'    context.g["codes"] = {code_list}\n'
        '    context.g["done"] = False\n\n'
        "def on_bar(context, bar):\n"
        '    if context.g["done"]:\n'
        "        return\n"
        '    context.g["done"] = True\n'
        "    total = context.account.total_value\n"
        '    for code in context.g["codes"]:\n'
        '        context.adapter.order_target_value(code, total / len(context.g["codes"]) * 0.9)\n'
    )


def _make_env(tmp_path: Path):
    data_root = tmp_path / "data"
    codes = _gen_etf_pool(data_root)
    strat = tmp_path / "buyhold.py"
    strat.write_text(_buy_hold_strategy(codes), encoding="utf-8")
    task = TaskConfig(
        task_name="perf_5y50",
        strategy={"file": str(strat), "type": "native", "entry": "on_bar"},
        backtest={
            "start": START,
            "end": (date.fromisoformat(START) + timedelta(days=N_DAYS * 1.4)).isoformat(),
            "initial_capital": 1_000_000.0,
        },
        universe=codes,
        fees={"commission_rate": 0.0001, "min_commission": 5.0, "stamp_tax_rate": 0.0},
    )
    settings = Settings(
        data=DataSettings(
            local_csv=LocalCsvSettings(root_path=str(data_root)),
            cache=CacheSettings(enabled=True, parquet_dir=str(tmp_path / ".cache" / "parquet")),
        ),
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'zq.db'}"),
    )
    return task, settings, tmp_path


@pytest.mark.slow
def test_tp01_full_pipeline_5y50_under_30s(tmp_path: Path) -> None:
    """T-P01: 50 标的×5 年全链路（加载+回测+入库）<30s; 二次 L2 命中数据准备 <2s。"""
    task, settings, tmp = _make_env(tmp_path)
    result = run_task(task, settings=settings, out_root=tmp / "results")
    timing = result.timing

    # 全链路 <30s（12.1-M1 验收）
    assert timing["total"] < 30.0, f"全链路 {timing['total']:.2f}s 超预算 30s: {timing}"
    # 结果完整（50 单 50 成交 + 全区间净值）
    assert len(result.bundle.fills) == N_CODES
    assert len(result.bundle.navs) == N_DAYS
    # 二次运行: L2 parquet 命中 → 数据准备 <2s（3.7 缓存收益）
    t0 = time.perf_counter()
    build_pipeline(settings, task.universe)
    prep2 = time.perf_counter() - t0
    assert prep2 < 2.0, f"二次数据准备 {prep2:.2f}s 超预算 2s"
    # 打印分解（性能验收留痕）
    print(
        f"\n[T-P01] load={timing['load']:.2f}s engine={timing['engine']:.2f}s "
        f"export={timing['export']:.2f}s persist={timing['persist']:.2f}s "
        f"total={timing['total']:.2f}s | 二次数据准备={prep2:.2f}s"
    )


@pytest.mark.slow
def test_tp02_db_write_does_not_block_engine(tmp_path: Path) -> None:
    """T-P02: 引擎纯计算占比>90%（写库异步化/后置, 8.7 不阻塞主循环）。"""
    task, settings, tmp = _make_env(tmp_path)
    result = run_task(task, settings=settings, out_root=tmp / "results")
    timing = result.timing
    engine_share = timing["engine"] / (timing["engine"] + timing["persist"] + 1e-9)
    assert engine_share > 0.90, (
        f"引擎纯计算占比 {engine_share:.1%} 过低（engine={timing['engine']:.2f}s, "
        f"persist={timing['persist']:.2f}s）——写库阻塞了主循环"
    )
    print(
        f"\n[T-P02] 引擎占比 {engine_share:.1%} "
        f"(engine={timing['engine']:.2f}s persist={timing['persist']:.2f}s)"
    )
