# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : T-X03 隔离：环境清洁（剔除 *_TOKEN/API_KEY）+ 超时进程树 terminate（设计 2.4）

"""T-X03：`--isolate` subprocess 隔离最小实现。

断言:
  环境清洁 —— clean_env 剔除 *_TOKEN/API_KEY/SECRET/PASSWORD/WEBHOOK, 保留正常变量
  超时     —— 超过 timeout 的极限任务被进程树 terminate（marker 文件未产生, returncode=124）
  CLI     —— `mtzquant run --isolate` 子进程经 MTZQUANT_SETTINGS 读到隔离配置并正常跑完
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from mtzquant.worker.isolate import clean_env, run_isolated
from tests.fixtures.backtest_env import make_backtest_env


def test_tx03_clean_env_removes_sensitive_vars() -> None:
    """环境清洁: 剔除敏感变量, 非敏感保留（3.6 密钥纪律）。"""
    env = {
        "PATH": "/usr/bin",
        "MTZQUANT_TUSHARE_TOKEN": "secret",
        "FOO_API_KEY": "k",
        "DB_PASSWORD": "p",
        "MY_SECRET": "s",
        "WEBHOOK_URL": "http://hook",
        "NORMAL_VAR": "ok",
    }
    clean = clean_env(env)
    assert "MTZQUANT_TUSHARE_TOKEN" not in clean
    assert "FOO_API_KEY" not in clean
    assert "DB_PASSWORD" not in clean
    assert "MY_SECRET" not in clean
    assert "WEBHOOK_URL" not in clean
    assert clean["PATH"] == "/usr/bin"
    assert clean["NORMAL_VAR"] == "ok"


def test_tx03_timeout_terminates_process_tree(tmp_path: Path) -> None:
    """超时: 极限任务被进程树 terminate（marker 未产生, 2.4 资源限额）。"""
    marker = tmp_path / "done.txt"
    code = f"import time; time.sleep(30); open({str(marker)!r}, 'w').write('done')"
    result = run_isolated([sys.executable, "-c", code], timeout_seconds=0.5)
    assert result.timed_out is True
    assert result.returncode == 124
    assert not marker.exists(), "进程树应被 terminate, 不产生 marker"


def test_tx03_cli_isolate_runs_subprocess(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CLI --isolate: 子进程经 MTZQUANT_SETTINGS 读到隔离配置并完成回测。"""
    env = make_backtest_env(tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(env.settings.model_dump_json(), encoding="utf-8")
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(env.task.model_dump(), ensure_ascii=False), encoding="utf-8")

    # 子进程需可 import mtzquant（非 pip 安装时经 PYTHONPATH=仓库根, 不属敏感变量被保留）
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", str(repo_root))
    monkeypatch.setenv("MTZQUANT_SETTINGS", str(settings_path))
    monkeypatch.setenv("TUSHARE_TOKEN", "must-be-cleaned-in-child")
    monkeypatch.setenv("MY_API_KEY", "must-be-cleaned-in-child")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(runner_cli_app(), ["run", "--isolate", "-c", str(task_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["isolated"] is True
    assert payload["timed_out"] is False
    assert payload["run"]["status"] == "completed_exact"
    assert payload["run"]["fills"] == 2  # 子进程真实跑出 2 笔成交


def runner_cli_app():
    """延迟导入 CLI app（避免 import 顺序副作用）。"""
    from mtzquant.cli import app

    return app
