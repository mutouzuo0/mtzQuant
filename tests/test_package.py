# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 21:33:45
# @update_time        : 2026/08/16 21:59:08
# @description : 阶段 A 冒烟测试：包可安装、配置加载/脱敏正确、CLI 入口可用

"""阶段 A 冒烟测试：包可安装、配置加载/脱敏正确、CLI 入口可用。

对应实施计划阶段 A 验收标准；用例编号遵循测试方案 §3 命名约定（阶段 A 无专项编号）。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mtzquant import METRICS_VERSION, __version__
from mtzquant.cli import app
from mtzquant.config import (
    get_tushare_token,
    load_secrets,
    load_settings,
    sanitize_params,
)

runner = CliRunner()


# ---------------- 包元信息 ----------------
def test_package_version() -> None:
    assert __version__ == "0.1.0"
    assert METRICS_VERSION == "1.0.0"


# ---------------- 配置加载（设计 3.6） ----------------
def test_load_settings_from_example(example_settings_path: Path) -> None:
    settings = load_settings(example_settings_path)
    assert settings.data.driver == "local_csv"
    assert settings.data.local_csv.root_path == "./data"
    assert settings.data.local_csv.format == "auto"
    assert settings.engine.fill_price == "next_open"
    assert settings.engine.max_participation == 0.25
    assert settings.engine.random_seed == 42
    assert settings.engine.default_fees.min_commission == 5.0
    assert settings.database.batch_size == 500
    assert settings.database.buffer_max_rows == 50000


def test_load_settings_missing_file_returns_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "nonexistent.json")
    assert settings.engine.fill_price == "next_open"
    assert settings.data.cache.warmup_bars_default == 120
    assert settings.data.download.dedup_keep == "latest"


def test_load_secrets(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"tushare": {"token": "file-token"}}), encoding="utf-8")
    assert load_secrets(path) == {"tushare": {"token": "file-token"}}
    assert load_secrets(tmp_path / "absent.json") == {}


def test_tushare_token_env_overrides_file(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"tushare": {"token": "file-token"}}), encoding="utf-8")
    secrets = load_secrets(path)
    assert get_tushare_token(secrets) == "file-token"
    monkeypatch.setenv("MTZQUANT_TUSHARE_TOKEN", "env-token")
    assert get_tushare_token(secrets) == "env-token"


def test_sanitize_params_masks_sensitive_keys() -> None:
    params = {
        "strategy": {"file": "strategies/demo.py"},
        "tushare_token": "real-secret",
        "api_key": "real-secret",
        "notify": {"wechat_webhook": "http://hook", "level": "info"},
        "password": "123",
        "backtest": {"start": "2020-01-01"},
    }
    out = sanitize_params(params)
    assert out["tushare_token"] == ""
    assert out["api_key"] == ""
    assert out["password"] == ""
    assert out["notify"]["wechat_webhook"] == ""
    # 非敏感字段原样保留
    assert out["strategy"]["file"] == "strategies/demo.py"
    assert out["notify"]["level"] == "info"
    assert out["backtest"]["start"] == "2020-01-01"


# ---------------- CLI（设计 10.1） ----------------
def test_cli_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "config", "fetch-etf", "replay"):
        assert cmd in result.output


def test_cli_run_missing_config_fails_gracefully(tmp_path: Path) -> None:
    """阶段 I 后 run 已实现: 缺失任务文件 → 结构化错误 + 退出码 1（非占位 2）。"""
    result = runner.invoke(app, ["run", "-c", str(tmp_path / "missing.json"), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "MtzQuantError"


def test_cli_config_check_with_missing_files(monkeypatch, tmp_path: Path) -> None:
    """本地配置缺失时 config check 仍应正常完成（降级提示而非崩溃）。"""
    import mtzquant.cli as cli_mod

    monkeypatch.setattr(cli_mod, "DEFAULT_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(cli_mod, "DEFAULT_SECRETS_PATH", tmp_path / "secrets.json")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
