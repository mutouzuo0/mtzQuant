# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : 阶段 I 测试共享装配：合成数据 + 策略 + 任务 + Settings（T-X01/X02/I01 共用）

"""阶段 I 测试装配（runner/session/replay/CLI 测试共用）。

`make_backtest_env(tmp_path)` 返回 BacktestEnv:
  data_root   合成 ETF CSV（tushare 源格式, 3.5）根目录
  settings    Settings（root_path=data_root, db url 指向 tmp）
  task        TaskConfig（native 策略: 第 5 根 bar 买、第 20 根卖）
  strategy_path  策略文件路径（策略内用 context.g["bars"] 计数, 确定性 8.8）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mtzquant.config import (
    CacheSettings,
    DatabaseSettings,
    DataSettings,
    LocalCsvSettings,
    Settings,
)
from mtzquant.engine.session import TaskConfig

from .synth import flat_etf_csv

# 确定性策略: 第 5 根 bar 买至 500k, 第 20 根 bar 清仓（T-X01 两跑逐笔一致的载体）
DETERMINISTIC_STRATEGY = """\
def initialize(context):
    context.g["code"] = "510300.SH"
    context.g["bars"] = 0


def on_bar(context, bar):
    context.g["bars"] += 1
    n = context.g["bars"]
    code = context.g["code"]
    if n == 5:
        context.adapter.order_target_value(code, 500_000)
    elif n == 20:
        context.adapter.order_target_value(code, 0.0)
"""


@dataclass
class BacktestEnv:
    """一次测试所需的完整装配。"""

    tmp: Path
    data_root: Path
    settings: Settings
    task: TaskConfig
    strategy_path: Path
    code: str = "510300.SH"

    @property
    def out_root(self) -> Path:
        return self.tmp / "results"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.tmp / 'zq.db'}"


def make_backtest_env(
    tmp_path: Path,
    *,
    strategy_text: str = DETERMINISTIC_STRATEGY,
    code: str = "510300.SH",
    n: int = 60,
    start: str = "2020-01-02",
    end: str = "2020-03-31",
    initial_capital: float = 1_000_000.0,
    price: float = 10.0,
    fees: dict[str, float] | None = None,
    task_name: str = "phase_i",
) -> BacktestEnv:
    """装配合成数据 + 策略 + 任务 + Settings（tmp_path 内隔离, 8.8 确定性）。"""
    tmp = Path(tmp_path)
    data_root = tmp / "data"
    flat_etf_csv(data_root, code, n=n, price=price)
    strategy_path = tmp / "strat.py"
    strategy_path.write_text(strategy_text, encoding="utf-8")
    task = TaskConfig(
        task_name=task_name,
        strategy={"file": str(strategy_path), "type": "native", "entry": "on_bar"},
        backtest={
            "start": start,
            "end": end,
            "initial_capital": initial_capital,
            "frequency": "1d",
        },
        universe=[code],
        fees=fees or {},
    )
    settings = Settings(
        data=DataSettings(
            local_csv=LocalCsvSettings(root_path=str(data_root)),
            cache=CacheSettings(enabled=True, parquet_dir=str(tmp / ".cache" / "parquet")),
        ),
        database=DatabaseSettings(url=f"sqlite:///{tmp / 'zq.db'}"),
    )
    return BacktestEnv(
        tmp=tmp,
        data_root=data_root,
        settings=settings,
        task=task,
        strategy_path=strategy_path,
        code=code,
    )
