# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 16:15:00
# @description : T-I01 CLI 端到端：validate → run → list → report（设计 10.1, 退出码 0/1）

"""T-I01：CLI 全链路（设计 10.1）。

validate  有效任务 exit 0; 非法任务 JSON → 精确字段报错 + exit 1（--json 可解析）
run       --json 输出 run_id/status; 导出物落盘; DB 入库
list      --json 可见刚跑出的 run（含 sharpe 排序列）
report    产出 report.html（含语义保真/交互净值canvas图/成交分页表）
replay    未篡改 → identical
退出码    成功 0 / 结构化错误 1
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tests.fixtures.backtest_env import make_backtest_env
from zquant.cli import app

runner = CliRunner()


def _write_env(tmp_path: Path):
    """落盘 settings.json（ZQUANT_SETTINGS）+ task.json, chdir 到 tmp。"""
    env = make_backtest_env(tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(env.settings.model_dump_json(), encoding="utf-8")
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(env.task.model_dump(), ensure_ascii=False), encoding="utf-8")
    return env, settings_path, task_path


def test_ti01_validate_ok_and_bad(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, settings_path, task_path = _write_env(tmp_path)
    monkeypatch.setenv("ZQUANT_SETTINGS", str(settings_path))
    monkeypatch.chdir(tmp_path)

    r = runner.invoke(app, ["validate", "-c", str(task_path)])
    assert r.exit_code == 0
    assert "任务配置有效" in r.output

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["validate", "-c", str(bad), "--json"])
    assert r.exit_code == 1
    payload = json.loads(r.output)
    assert payload["error"]["type"] == "ValidationError"
    assert any("task_name" in str(e.get("loc")) for e in payload["error"]["errors"])


def test_ti01_run_list_report(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, settings_path, task_path = _write_env(tmp_path)
    monkeypatch.setenv("ZQUANT_SETTINGS", str(settings_path))
    monkeypatch.chdir(tmp_path)

    # run（--json 机读）
    r = runner.invoke(app, ["run", "-c", str(task_path), "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    run_id = payload["run_id"]
    assert payload["status"] == "completed_exact"
    assert payload["fills"] == 2

    # 导出物落盘（9.1）
    run_dir = tmp_path / "results" / run_id
    for fname in ("daily_stats.csv", "orders.csv", "fills.csv", "summary.json", "manifest.json"):
        assert (run_dir / fname).is_file(), f"缺导出物 {fname}"

    # list（DB 可见, 含 run_id）
    r = runner.invoke(app, ["list", "--json"])
    assert r.exit_code == 0
    listed = json.loads(r.output)
    assert any(row["run_id"] == run_id for row in listed)

    # report（report.html 自包含单文件）
    r = runner.invoke(app, ["report", run_id])
    assert r.exit_code == 0
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "completed_exact" in html
    assert 'id="nav-chart"' in html  # 交互净值图 canvas
    assert 'id="orders-body"' in html  # 成交分页表

    # report 不存在的 run → exit 1
    r = runner.invoke(app, ["report", "r_does_not_exist", "--json"])
    assert r.exit_code == 1


def test_ti01_replay_identical(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, settings_path, task_path = _write_env(tmp_path)
    monkeypatch.setenv("ZQUANT_SETTINGS", str(settings_path))
    monkeypatch.chdir(tmp_path)

    r = runner.invoke(app, ["run", "-c", str(task_path), "--json"])
    run_id = json.loads(r.output)["run_id"]

    r = runner.invoke(app, ["replay", run_id, "--json"])
    assert r.exit_code == 0
    rep = json.loads(r.output)
    assert rep["identical"] is True
    assert rep["manifest_identical"] is True
