# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 16:30:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-C01..C03：compare 对照表/lineage 树/rerun 谱系指向/diff（设计 10.3）

"""T-C01..C03（M2-P1/P2, 设计 10.3）。

compare   多 run 8.4 指标对照表（--json 机读 + best 标注）
lineage   parent_run_id 谱系树（rerun 后新 run 指向原 run）
diff      策略源码/参数差异（sort_keys 归一）
rerun     params_json 原样重跑, 新 run parent_run_id 指向原 run
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mtzquant.cli import app
from mtzquant.engine.runner import run_task
from tests.fixtures.backtest_env import make_backtest_env

runner = CliRunner()


def _prepare(tmp_path: Path, *, price: float = 10.0) -> tuple[str, str]:
    """落盘 settings（DB 指向 tmp）+ 两次 run（不同参数 → 不同指标/净值）。"""
    from mtzquant.config import DatabaseSettings

    env = make_backtest_env(tmp_path, price=price)
    db_url = f"sqlite:///{tmp_path / 'zq.db'}"
    settings = env.settings.model_copy(update={"database": DatabaseSettings(url=db_url)})
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(settings.model_dump_json(), encoding="utf-8")

    r1 = run_task(env.task, settings=settings, out_root=tmp_path / "results", db_url=db_url)
    env2 = make_backtest_env(tmp_path, price=price + 1.0)
    env2.task.task_name = "p2"
    env2.task.backtest.initial_capital = 2_000_000  # 参数不同 → params_diff 有内容
    r2 = run_task(env2.task, settings=settings, out_root=tmp_path / "results", db_url=db_url)
    return settings_path, r1.run_id, r2.run_id


def _invoke(tmp_path, settings_path, args, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MTZQUANT_SETTINGS", str(settings_path))
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, args)


def test_tc01_compare_table(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings_path, r1, r2 = _prepare(tmp_path)
    r = _invoke(tmp_path, settings_path, ["compare", r1, r2, "--json"], monkeypatch)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert set(payload["metrics"]) >= {"夏普 sharpe", "总收益 total_return"}
    assert r1 in payload["metrics"]["夏普 sharpe"] and r2 in payload["metrics"]["夏普 sharpe"]
    assert r1 in payload["navs"] and r2 in payload["navs"]  # 净值对齐


def test_tc02_lineage_tree(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings_path, r1, r2 = _prepare(tmp_path)
    r = _invoke(tmp_path, settings_path, ["lineage", "--json"], monkeypatch)
    assert r.exit_code == 0, r.output
    nodes = json.loads(r.output)["nodes"]
    ids = {n["run_id"] for n in nodes}
    assert r1 in ids and r2 in ids


def test_tc02_rerun_parent_lineage(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings_path, r1, r2 = _prepare(tmp_path)
    r = _invoke(tmp_path, settings_path, ["rerun", r1, "--json"], monkeypatch)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["parent_run_id"] == r1
    new_id = payload["run_id"]
    assert new_id != r1  # 新 run_id
    # lineage 树含新节点, 其 parent 指向 r1
    rl = _invoke(tmp_path, settings_path, ["lineage", "--json"], monkeypatch)
    nodes = json.loads(rl.output)["nodes"]
    child = next(n for n in nodes if n["run_id"] == new_id)
    assert child["parent_run_id"] == r1


def test_tc03_diff_strategy_and_params(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings_path, r1, r2 = _prepare(tmp_path)
    r = _invoke(tmp_path, settings_path, ["diff", r1, r2, "--json"], monkeypatch)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert "strategy_diff" in payload and "params_diff" in payload
    # 不同参数 → params_diff 有内容（初始资金/价格不同）
    assert any(("+" in line or "-" in line) for line in payload["params_diff"].splitlines())
