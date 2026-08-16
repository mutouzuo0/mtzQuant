# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 15:40:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-C04 CLI：fetch（dry-run/import/幂等）+ sql 只读守卫（设计 10.1, 退出码 0/1）

"""T-C04：CLI fetch + sql 端到端（设计 10.1/3.9/3.10）。

sql     SELECT 放行 exit 0; 写面语句（DELETE/INSERT/PRAGMA）拒绝 exit 1（3.10 只读）
fetch   --dry-run 仅覆盖检查不写盘（exit 0）; --import 导入本地 CSV 入库;
        重复 fetch 幂等（同参数 → 零下载零写入, 3.9）
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mtzquant.cli import app
from mtzquant.config import Settings

runner = CliRunner()

CODE = "510300.SH"
CSV_HEADER = "ts_code,trade_date,open,high,low,close,vol,amount\n"
ROW = "510300.SH,20240102,3.50,3.60,3.40,3.55,1200000,4200000\n"


def _write_env(tmp_path: Path) -> Path:
    """落盘 settings.json（root_path=tmp/data）+ chdir 准备。"""
    settings = Settings(data__local_csv__root_path=str(tmp_path / "data"))
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(settings.model_dump_json(), encoding="utf-8")
    return settings_path


def _kline_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "kline" / "etf" / "day" / f"{CODE}.csv"


def test_tc04_sql_select_and_write_guard(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _kline_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _kline_path(tmp_path).write_text(CSV_HEADER + ROW, encoding="utf-8")
    settings_path = _write_env(tmp_path)
    monkeypatch.setenv("MTZQUANT_SETTINGS", str(settings_path))
    monkeypatch.chdir(tmp_path)

    r = runner.invoke(
        app,
        ["sql", "SELECT trade_date FROM read_csv_auto('data/kline/etf/day/*.csv')"],
    )
    assert r.exit_code == 0, r.output
    assert "20240102" in r.output

    for bad in ("DELETE FROM x", "INSERT INTO x VALUES(1)", "PRAGMA database_list"):
        r = runner.invoke(app, ["sql", bad, "--json"])
        assert r.exit_code == 1, f"应拒绝写面语句: {bad}"
        payload = json.loads(r.output)
        assert payload["error"]["type"] == "MtzQuantError"


def test_tc04_fetch_dry_run_and_import(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings_path = _write_env(tmp_path)
    monkeypatch.setenv("MTZQUANT_SETTINGS", str(settings_path))
    monkeypatch.chdir(tmp_path)

    # dry-run: 仅覆盖检查, 不下载不写盘
    r = runner.invoke(
        app,
        [
            "fetch",
            "--codes",
            CODE,
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-31",
            "--dry-run",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload[0]["status"] == "dry_run"
    assert not _kline_path(tmp_path).exists()  # 未写盘

    # import: 本地 CSV → 嗅探 → 去重合并入库
    src = tmp_path / "import"
    src.mkdir()
    (src / f"{CODE}.csv").write_text(CSV_HEADER + ROW, encoding="utf-8")
    r = runner.invoke(app, ["fetch", "--import", str(src), "--json"])
    assert r.exit_code == 0, r.output
    assert _kline_path(tmp_path).is_file()
    content = _kline_path(tmp_path).read_text(encoding="utf-8")
    assert "20240102" in content
