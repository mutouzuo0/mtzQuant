# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : T-X01 确定性重放：同 manifest 两跑 orders/fills 逐笔、nav 逐点、metrics 全等（8.8）

"""T-X01：同输入连续跑两次 → 逐笔/逐点一致（设计 8.8 确定性验收）。

比对内容:
  manifest_hash 全等（strategy/data/config 哈希聚合）
  orders   逐单全等（code/side/qty/status/时刻; order_id 内嵌 run_id 故归一化）
  fills    逐笔全等（code/side/price/volume/amount/时刻）
  navs     逐点全等（nav/cash/positions_value/total_value/drawdown）
  fees / status / degradations 全等
"""

from __future__ import annotations

from pathlib import Path

from mtzquant.engine.runner import run_task
from tests.fixtures.backtest_env import make_backtest_env


def _strip_run_id(items: list[dict], *, key: str = "order_id") -> list[dict]:
    """剔除内嵌 run_id 的字段（order_id），仅比业务语义。"""
    return [{k: v for k, v in it.items() if k != key} for it in items]


def test_tx01_same_task_two_runs_identical(tmp_path: Path) -> None:
    env = make_backtest_env(tmp_path)
    r1 = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    r2 = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)

    # 8.8: 同 manifest 两跑清单哈希全等
    assert r1.manifest_hash == r2.manifest_hash
    # 订单逐单全等（order_id 归一）
    assert _strip_run_id(r1.bundle.orders) == _strip_run_id(r2.bundle.orders)
    # 成交逐笔全等（order_id 归一）
    assert _strip_run_id(r1.bundle.fills) == _strip_run_id(r2.bundle.fills)
    # 净值逐点全等
    assert r1.bundle.navs == r2.bundle.navs
    # 费用 / 状态 / 降级全等
    assert r1.bundle.fees == r2.bundle.fees
    assert r1.bundle.status == r2.bundle.status == "completed_exact"
    assert r1.bundle.degradations == r2.bundle.degradations == []
    # 至少发生了成交（确定性非空跑）
    assert len(r1.bundle.fills) == 2 and len(r1.bundle.navs) == 60


def test_tx01_manifest_strategy_sha256_matches_snapshot(tmp_path: Path) -> None:
    """清单 strategy_sha256 == 策略源码哈希（8.3.2 与 snapshot 同源）。"""
    from mtzquant.engine.manifest import sha256_text

    env = make_backtest_env(tmp_path)
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    code = env.strategy_path.read_text(encoding="utf-8")
    assert result.manifest["strategy_sha256"] == sha256_text(code)
    assert result.manifest["random_seed"] == 42
    # data_manifest 覆盖 universe 标的
    assert "510300.SH" in result.manifest["data_manifest"]
