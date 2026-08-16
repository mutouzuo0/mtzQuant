# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 03:10:00
# @update_time        : 2026/08/17 03:10:00
# @description : M4-Z1/Y3 测试：数据体检扫描 + remote 检测分支（3.10/7.7, 13.5）

"""T-Z01（数据体检报告字段）与 T-Y03（remote 检测分支）。"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from mtzquant.cli import app as cli_app
from mtzquant.data.health import scan_health
from tests.fixtures.synth import write_etf_csv

runner = CliRunner()


def _write_bad_csv(root: Path, code: str) -> Path:
    """写一张含 0 价 0 量 + 重复日期的坏 K 线 CSV（体检应检出）。"""
    p = root / "kline" / "etf" / "day" / f"{code}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "ts_code,trade_date,open,high,low,close,vol,amount\n"
        f"{code},20200102,10,10,10,10,0,0\n"  # 0 量
        f"{code},20200103,10,12,8,11,1000,11000\n"
        f"{code},20200103,10,12,8,11,1000,11000\n"  # 重复 dt
        f"{code},20200106,0,0,0,0,0,0\n",  # 0 价 0 量
        encoding="utf-8",
    )
    return p


class TestHealthScan:
    def test_scan_clean_and_dirty(self, tmp_path: Path) -> None:
        write_etf_csv(tmp_path, "510300.SH", n=30)
        _write_bad_csv(tmp_path, "510500.SH")
        report = scan_health(tmp_path)
        by_code = {h.code: h for h in report.instruments}
        assert "510300.SH" in by_code
        assert "510500.SH" in by_code
        bad = by_code["510500.SH"]
        # 重复日期检出
        assert "duplicate_dates" in bad.checks
        assert bad.checks["duplicate_dates"]["n"] == 1
        # 0 价 0 量检出
        assert "zero_price_volume" in bad.checks
        assert bad.checks["zero_price_volume"]["n"] >= 1
        # 干净标的无异常
        assert by_code["510300.SH"].checks == {}

    def test_health_json_shape(self, tmp_path: Path) -> None:
        write_etf_csv(tmp_path, "510300.SH", n=20)
        data = scan_health(tmp_path).to_dict()
        assert {"scanned_at", "instruments", "issue_count", "items"} <= set(data)
        assert data["instruments"] == 1
        assert isinstance(data["items"], list)

    def test_health_cli(self, tmp_path: Path) -> None:
        write_etf_csv(tmp_path, "510300.SH", n=20)
        import os

        os.environ["MTZQUANT_SETTINGS"] = str(tmp_path / "settings.json")
        try:
            (tmp_path / "settings.json").write_text(
                '{"data":{"local_csv":{"root_path":"' + str(tmp_path).replace("\\", "/") + '"}}}',
                encoding="utf-8",
            )
            res = runner.invoke(cli_app, ["health", "--json"])
            import json

            data = json.loads(res.output)
            assert data["instruments"] == 1
        finally:
            os.environ.pop("MTZQUANT_SETTINGS", None)


class TestRemoteCommand:
    def test_tailscale_not_installed_exit1(self) -> None:
        res = runner.invoke(cli_app, ["remote", "--provider", "tailscale", "--json"])
        # 未装 tailscale → 退出码 1, 机读 installed:false
        assert res.exit_code == 1
        import json

        data = json.loads(res.output)
        assert data == {"provider": "tailscale", "installed": False}

    def test_unknown_provider_guidance(self) -> None:
        res = runner.invoke(cli_app, ["remote", "--provider", "ngrok"])
        assert "ngrok" in res.output
        assert res.exit_code == 0
