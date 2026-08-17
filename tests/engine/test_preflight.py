# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 10:30:00
# @update_time        : 2026/08/17 10:30:00
# @description : backtest 预检测试：标的检测/完整性/缺失下载/Web 地址 + WsHub 回放修复回归

"""`mtzquant backtest` 预检（计划: 检测标的→数据完整性→缺则下载→回测→Web 地址）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from mtzquant.cli import app as cli_app
from mtzquant.config import DatabaseSettings, Settings
from mtzquant.engine.preflight import (
    check_data,
    detect_codes,
    ensure_data,
    required_codes,
    web_url,
)
from mtzquant.server.ws import WsHub
from tests.fixtures.synth import write_etf_csv

runner = CliRunner()

_REPO = Path(__file__).resolve().parents[2]


def _strategy_source(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


# ============================================================
# detect_codes / required_codes
# ============================================================
class TestDetectCodes:
    def test_native_dual_ma(self) -> None:
        assert detect_codes(_strategy_source("strategies/native/dual_ma.py")) == ["510300.SH"]

    def test_joinquant_and_ptrade_suffix(self) -> None:
        assert detect_codes(_strategy_source("strategies/joinquant/dual_ma.py")) == ["510300.SH"]
        assert detect_codes(_strategy_source("strategies/ptrade/demo_all_weather.py")) == [
            "510300.SH"
        ]

    def test_all_weather_excludes_comments_and_benchmark(self) -> None:
        """全天候 14 个标的; 行内注释 159915 与 set_benchmark 基准 000300 均不入列。"""
        codes = detect_codes(_strategy_source("strategies/ptrade/全天候策略_ptrade.py"))
        assert len(codes) == 14
        assert "159915.SZ" not in codes  # 注释掉
        assert "000300.SH" not in codes  # 基准（set_benchmark）
        assert "510310.SH" in codes and "165513.SZ" in codes

    def test_comments_and_inline_comment(self) -> None:
        src = "# 159915.XSHE 注释\nx = '510300.XSHG'  # 行内\nset_benchmark('000300.XSHG')\n"
        assert detect_codes(src) == ["510300.SH"]

    def test_qmt_market_code(self) -> None:
        assert detect_codes("c = '1.600000'\n") == ["600000.SH"]


class TestRequiredCodes:
    def test_merge_universe_detected_benchmark(self, tmp_path: Path) -> None:
        from mtzquant.engine.session import TaskConfig

        task = TaskConfig.model_validate(
            {
                "task_name": "t",
                "strategy": {"file": str(tmp_path / "s.py"), "type": "native"},
                "backtest": {
                    "start": "2020-01-01",
                    "end": "2020-03-01",
                    "initial_capital": 1e6,
                    "benchmark": "000300.XSHG",
                },
                "universe": ["510300.SH"],
            }
        )
        source = "g.code='510500.XSHG'\nset_benchmark('000300.XSHG')\n"
        codes = required_codes(task, strategy_source=source)
        # universe(510300) ∪ 检测(510500) ∪ 基准(000300, 来自 task.backtest.benchmark)
        assert codes == ["000300.SH", "510300.SH", "510500.SH"]


# ============================================================
# check_data / ensure_data（注入 fetch_fn 只下缺失段）
# ============================================================
class TestDataPreflight:
    def _settings(self, tmp_path: Path) -> Settings:
        s = Settings(database=DatabaseSettings(url=f"sqlite:///{tmp_path / 't.db'}"))
        s.data.local_csv.root_path = str(tmp_path)
        return s

    def test_check_data_complete_and_missing(self, tmp_path: Path) -> None:
        write_etf_csv(tmp_path, "510300.SH", n=80)  # 覆盖到 ~2020-04-24 → 区间完整
        # 510500.SH 不写 → 全缺失
        s = self._settings(tmp_path)
        rep = check_data(s, ["510300.SH", "510500.SH"], date(2020, 1, 2), date(2020, 3, 31))
        assert rep.ok == ["510300.SH"]
        assert "510500.SH" in rep.missing
        assert not rep.complete

    def test_ensure_data_downloads_only_missing(self, tmp_path: Path) -> None:
        write_etf_csv(tmp_path, "510300.SH", n=80)
        fetched: list[str] = []

        def fetch_fn(code: str, start: date, end: date, *, source: str, instrument_type: str):
            fetched.append(code)
            from tests.fixtures.synth import trade_days

            days = trade_days(start, 64)  # 覆盖请求区间
            return __import__("pandas").DataFrame(
                {
                    "ts_code": [code] * len(days),
                    "trade_date": [d.strftime("%Y%m%d") for d in days],
                    "open": [10.0] * len(days),
                    "high": [10.0] * len(days),
                    "low": [10.0] * len(days),
                    "close": [10.0] * len(days),
                    "vol": [1e6] * len(days),
                    "amount": [1e7] * len(days),
                }
            )

        s = self._settings(tmp_path)
        rep = ensure_data(
            s, ["510300.SH", "510500.SH"], date(2020, 1, 2), date(2020, 3, 31), fetch_fn=fetch_fn
        )
        assert fetched == ["510500.SH"]  # 只下缺失标的
        assert rep.complete
        assert rep.downloaded == ["510500.SH"]

    def test_check_only_does_not_download(self, tmp_path: Path) -> None:
        fetched: list[str] = []

        def fetch_fn(code, start, end, *, source, instrument_type):
            fetched.append(code)
            return __import__("pandas").DataFrame()

        s = self._settings(tmp_path)
        rep = ensure_data(
            s,
            ["510500.SH"],
            date(2020, 1, 2),
            date(2020, 3, 31),
            auto_fetch=False,
            fetch_fn=fetch_fn,
        )
        assert fetched == []
        assert not rep.complete
        assert rep.check_only


# ============================================================
# web_url
# ============================================================
class TestWebUrl:
    def test_url_from_settings(self) -> None:
        s = Settings()
        s.server.host = "0.0.0.0"
        s.server.port = 9000
        assert web_url("r_abc", s) == "http://0.0.0.0:9000/#/monitor?run=r_abc"

    def test_url_defaults(self) -> None:
        assert web_url("r_x", Settings()) == "http://127.0.0.1:8501/#/monitor?run=r_x"


# ============================================================
# CLI backtest 端到端（合成完整数据 → run_id + url + 预检报告）
# ============================================================
class TestBacktestCli:
    def test_backtest_json_end_to_end(self, tmp_path: Path) -> None:
        import os

        write_etf_csv(tmp_path, "510300.SH", n=80)  # 覆盖区间 → 预检 complete, 无需下载
        strategy = tmp_path / "s.py"
        strategy.write_text(
            "def initialize(c): c.g['code']='510300.SH'\n"
            "def on_bar(c, bar):\n"
            "    code=c.g['code']\n"
            "    if c.g.get('n',0)==0:\n"
            "        c.adapter.order_target_value(code, 500000)\n"
            "    c.g['n']=c.g.get('n',0)+1\n",
            encoding="utf-8",
        )
        task = {
            "task_name": "bt",
            "strategy": {"file": str(strategy), "type": "native"},
            "backtest": {
                "start": "2020-01-02",
                "end": "2020-03-31",
                "initial_capital": 1_000_000,
                "frequency": "1d",
            },
            "universe": ["510300.SH"],
            "fees": {
                "commission_rate": 0.0001,
                "min_commission": 5.0,
                "stamp_tax_rate": 0.0,
                "transfer_fee_rate": 0.0,
            },
            "engine": {},
        }
        task_path = tmp_path / "task.json"
        task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            '{"data":{"local_csv":{"root_path":"'
            + str(tmp_path).replace("\\", "/")
            + '"}},"database":{"url":"sqlite:///'
            + str(tmp_path).replace("\\", "/")
            + '/bt.db"},"server":{"host":"127.0.0.1","port":8501}}',
            encoding="utf-8",
        )
        os.environ["MTZQUANT_SETTINGS"] = str(settings_path)
        try:
            res = runner.invoke(cli_app, ["backtest", "-c", str(task_path), "--json"])
        finally:
            os.environ.pop("MTZQUANT_SETTINGS", None)
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["detected_codes"] == ["510300.SH"]
        assert data["preflight"]["complete"] is True
        assert data["run_id"].startswith("r_")
        assert data["status"] in ("completed_exact", "completed_degraded")
        assert data["url"].startswith("http://127.0.0.1:8501/#/monitor?run=")


# ============================================================
# WsHub 回放修复回归: seq=0 全量回放 journal（回测结束后新开监控页可看）
# ============================================================
class TestWsHubReplayFromZero:
    def test_replay_all_on_seq_zero(self) -> None:
        import asyncio

        journal = {
            "r_1": [
                {
                    "type": "daily_nav",
                    "run_id": "r_1",
                    "event_seq": i,
                    "committed": True,
                    "data": {},
                }
                for i in range(1, 4)
            ]
        }
        hub = WsHub(
            journal_loader=lambda run_id, after: [
                e for e in journal[run_id] if e["event_seq"] > after
            ]
        )
        loop = asyncio.new_event_loop()

        async def _run() -> None:
            # 新连接 last_event_seq=0 → 全量回放（修复前只回放 >0 的事件, 新页面看不到）
            q = hub.connect(run_id="r_1", last_event_seq=0)
            got = []
            while not q.empty():
                got.append(json.loads(q.get_nowait()))
            assert [e["event_seq"] for e in got] == [1, 2, 3]

        loop.run_until_complete(_run())
        loop.close()
