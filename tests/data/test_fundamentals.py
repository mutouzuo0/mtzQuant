# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 23:30:00
# @update_time        : 2026/08/16 23:30:00
# @description : M3-R 测试 T-R01..R03：财务三时间 PIT / 落盘幂等修订行 / 成分防幸存者偏差

"""T-R01..R03（设计 3.13, M3-R 研究数据层）——基本面 PIT 与成分快照。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError
from mtzquant.data.fetcher import DataFetcher
from mtzquant.data.fundamentals import FundamentalsStore
from mtzquant.data.provider import MarketDataProvider
from tests.fixtures.synth import write_etf_csv

_SH = ZoneInfo("Asia/Shanghai")


def _at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 15, 0, tzinfo=_SH)


def _write_fina(root: Path, code: str, rows: list[tuple[str, str, float, float]]) -> Path:
    """写 fina_indicator 源格式 CSV（ann_date, end_date, netprofit_yoy, or_yoy）。"""
    df = pd.DataFrame(
        [
            {"ts_code": code, "ann_date": a, "end_date": e, "netprofit_yoy": ny, "or_yoy": oy}
            for a, e, ny, oy in rows
        ]
    )
    p = root / "fundamentals" / "fina_indicator" / f"{code}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    return p


def _write_daily_basic(root: Path, code: str, rows: list[tuple[str, float, float, float]]) -> Path:
    df = pd.DataFrame(
        [
            {"ts_code": code, "trade_date": td, "pe": pe, "pb": pb, "total_mv": mv}
            for td, pe, pb, mv in rows
        ]
    )
    p = root / "fundamentals" / "daily_basic" / f"{code}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    return p


def _write_snapshot(
    root: Path, index: str, snap_date: date, members: list[tuple[str, str, str]]
) -> Path:
    """成分快照 CSV: (con_code, in_date, out_date)。"""
    df = pd.DataFrame(
        [
            {"index_code": index, "con_code": c, "in_date": i, "out_date": o, "weight": 1.0}
            for c, i, o in members
        ]
    )
    p = root / "index_constituents" / f"{index}_{snap_date:%Y%m%d}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    return p


# ============================================================
# T-R01: 三时间 PIT 正确（event_time/published_at/available_at）
# ============================================================
class TestPitThreeTime:
    def test_fina_indicator_visibility_by_published(self, tmp_path: Path) -> None:
        store = FundamentalsStore(tmp_path)
        _write_fina(
            tmp_path,
            "600000.SH",
            [
                ("20200430", "20200331", 10.5, 8.0),  # 2020Q1, 2020-04-30 披露
                ("20210430", "20210331", 20.0, 15.0),  # 2021Q1, 2021-04-30 披露
                ("20211028", "20210930", -5.0, 2.0),  # 2021Q3, 2021-10-28 披露
            ],
        )
        # as_of=2021-05-15: 报告期<=5/15 且 披露日<=5/15 → 前两行可见, Q3 不可见
        df = store.fundamentals("600000.SH", "fina_indicator", None, as_of=_at(2021, 5, 15))
        assert len(df) == 2
        assert df.index.date.tolist() == [date(2020, 3, 31), date(2021, 3, 31)]
        assert list(df["netprofit_yoy"]) == [10.5, 20.0]

    def test_knowledge_time_earlier_than_published_hides_row(self, tmp_path: Path) -> None:
        store = FundamentalsStore(tmp_path)
        _write_fina(
            tmp_path,
            "600000.SH",
            [("20200430", "20200331", 10.5, 8.0), ("20210430", "20210331", 20.0, 15.0)],
        )
        # knowledge_time=2021-04-25: 2021Q1 虽报告期已到但披露日在知识时间之后 → 不可见
        df = store.fundamentals(
            "600000.SH",
            "fina_indicator",
            None,
            as_of=_at(2021, 5, 15),
            knowledge_time=_at(2021, 4, 25),
        )
        assert len(df) == 1
        assert df.index.date.tolist() == [date(2020, 3, 31)]

    def test_available_at_offset_delay(self, tmp_path: Path) -> None:
        # 供应商同步延迟: available_at = ann_date + 5 天 → 知识时间需 >= 2021-05-05
        store = FundamentalsStore(tmp_path, delay_offset_days=5)
        _write_fina(tmp_path, "600000.SH", [("20210430", "20210331", 20.0, 15.0)])
        early = store.fundamentals(
            "600000.SH",
            "fina_indicator",
            None,
            as_of=_at(2021, 5, 15),
            knowledge_time=_at(2021, 5, 4),
        )
        assert early.empty  # 同步延迟内不可见
        late = store.fundamentals(
            "600000.SH",
            "fina_indicator",
            None,
            as_of=_at(2021, 5, 15),
            knowledge_time=_at(2021, 5, 5),
        )
        assert len(late) == 1

    def test_daily_basic_event_equals_published(self, tmp_path: Path) -> None:
        store = FundamentalsStore(tmp_path)
        _write_daily_basic(
            tmp_path,
            "600000.SH",
            [
                ("20200102", 8.0, 1.2, 1e5),
                ("20210104", 9.0, 1.5, 2e5),
                ("20220104", 10.0, 1.8, 3e5),
            ],
        )
        df = store.fundamentals("600000.SH", "daily_basic", ["pe", "pb"], as_of=_at(2021, 6, 1))
        assert len(df) == 2  # 2020-01-02 与 2021-01-04（2022 在未来不可见）
        assert df.index.date.tolist() == [date(2020, 1, 2), date(2021, 1, 4)]

    def test_missing_ann_date_fails_loud(self, tmp_path: Path) -> None:
        """ann_date 缺失行拒绝（fail-loud, R1 决策）而非填默认。"""
        store = FundamentalsStore(tmp_path)
        _write_fina(
            tmp_path,
            "600000.SH",
            [("20200430", "20200331", 10.5, 8.0), ("", "20201231", 5.0, 4.0)],
        )
        with pytest.raises(MtzQuantError, match="缺 ann_date"):
            store.fundamentals("600000.SH", "fina_indicator", None, as_of=_at(2021, 5, 15))

    def test_unknown_table_rejected(self, tmp_path: Path) -> None:
        store = FundamentalsStore(tmp_path)
        with pytest.raises(MtzQuantError, match="未知财务表"):
            store.fundamentals("600000.SH", "nope", None, as_of=_at(2021, 5, 15))


# ============================================================
# T-R02: 落盘幂等 / 修订版本行追加（DataFetcher 通道）
# ============================================================
class TestFundamentalsFetcher:
    def _fetcher(self, tmp_path: Path) -> DataFetcher:
        def fetch_fund_fn(code: str, table: str, start: date, end: date, *, source: str):
            del source
            if table == "fina_indicator":
                return pd.DataFrame(
                    {
                        "ts_code": [code, code],
                        "ann_date": ["20210430", "20211028"],
                        "end_date": ["20210331", "20210930"],
                        "netprofit_yoy": [20.0, -5.0],
                        "or_yoy": [15.0, 2.0],
                    }
                )
            return pd.DataFrame(
                {
                    "ts_code": [code],
                    "trade_date": ["20210104"],
                    "pe": [9.0],
                    "pb": [1.5],
                    "total_mv": [2e5],
                    "circ_mv": [1.6e5],
                }
            )

        return DataFetcher(tmp_path, fetch_fund_fn=fetch_fund_fn, sources=("tushare",))

    def test_fetch_fina_indicator_idempotent(self, tmp_path: Path) -> None:
        fetcher = self._fetcher(tmp_path)
        reps = fetcher.fetch_fundamentals(
            ["600000.SH"], "fina_indicator", date(2021, 1, 1), date(2021, 12, 31)
        )
        assert reps[0].status == "ok"
        assert reps[0].added_rows == 2
        path = tmp_path / "fundamentals" / "fina_indicator" / "600000.SH.csv"
        assert path.is_file()
        # 幂等: 再拉同区间（本地 ann_date 覆盖请求区间）→ skipped（本地已覆盖）
        reps2 = fetcher.fetch_fundamentals(
            ["600000.SH"], "fina_indicator", date(2021, 4, 30), date(2021, 10, 28)
        )
        assert reps2[0].status == "skipped"

    def test_daily_basic_merge_dedup(self, tmp_path: Path) -> None:
        fetcher = self._fetcher(tmp_path)
        fetcher.fetch_fundamentals(
            ["600000.SH"], "daily_basic", date(2021, 1, 1), date(2021, 12, 31)
        )
        # 修订数据（同交易日新值）→ 去重保留最新
        df = pd.read_csv(tmp_path / "fundamentals" / "daily_basic" / "600000.SH.csv")
        assert len(df) == 1
        assert df["pe"].iloc[0] == 9.0

    def test_revision_rows_kept_fina_indicator(self, tmp_path: Path) -> None:
        """修订版本行保留: 同 end_date 不同 ann_date → 两行都留（主键含公告日）。"""
        fetcher = self._fetcher(tmp_path)
        fetcher.fetch_fundamentals(
            ["600000.SH"], "fina_indicator", date(2021, 1, 1), date(2021, 12, 31)
        )

        # 追加一条同报告期但更新披露日的修订行（新注入范围超出本地 → 拉取）
        def fetch_rev(code: str, table: str, start: date, end: date, *, source: str):
            del code, table, source
            return pd.DataFrame(
                {
                    "ts_code": ["600000.SH"],
                    "ann_date": ["20211101"],
                    "end_date": ["20210930"],  # 同 2021Q3, 更晚披露 = 修订
                    "netprofit_yoy": [3.0],
                    "or_yoy": [1.0],
                }
            )

        fetcher._fetch_fund_fn = fetch_rev  # type: ignore[assignment]
        # 直接合并验证（绕过覆盖粗判: 请求更晚区间触发拉取）
        reps = fetcher.fetch_fundamentals(
            ["600000.SH"], "fina_indicator", date(2021, 10, 1), date(2021, 12, 31)
        )
        assert reps[0].status == "ok"
        df = pd.read_csv(tmp_path / "fundamentals" / "fina_indicator" / "600000.SH.csv", dtype=str)
        # 3 行: 2021Q1 + 2021Q3(20211028) + 2021Q3 修订(20211101)
        assert len(df) == 3
        assert set(df["ann_date"]) == {"20210430", "20211028", "20211101"}

    def test_fetch_constituents(self, tmp_path: Path) -> None:
        def fetch_cons(index: str, snap_date: date, *, source: str):
            del source
            assert index == normalize_code("000300.SH")
            return pd.DataFrame(
                {
                    "index_code": [index, index],
                    "con_code": ["600000.SH", "000001.SZ"],
                    "in_date": ["20150101", "20150101"],
                    "out_date": ["", ""],
                    "weight": [0.5, 0.5],
                }
            )

        fetcher = DataFetcher(tmp_path, fetch_constituents_fn=fetch_cons, sources=("tushare",))
        reps = fetcher.fetch_constituents(["000300.SH"], date(2021, 6, 1))
        assert reps[0].status == "ok"
        assert reps[0].members == 2
        path = tmp_path / "index_constituents" / "000300.SH_20210601.csv"
        assert path.is_file()
        # 幂等: 已存在 → skipped
        reps2 = fetcher.fetch_constituents(["000300.SH"], date(2021, 6, 1))
        assert reps2[0].status == "skipped"


# ============================================================
# T-R03: 防幸存者偏差（Provider.index_stocks 取历史快照）
# ============================================================
class TestSurvivorshipBias:
    def _provider(self, tmp_path: Path) -> MarketDataProvider:
        write_etf_csv(tmp_path, "000300.SH", n=40)  # 行情兜底（Provider 需 calendar/行情）
        write_etf_csv(tmp_path, "600000.SH", n=40)
        write_etf_csv(tmp_path, "000001.SZ", n=40)
        _write_snapshot(
            tmp_path,
            "000300.SH",
            date(2020, 1, 2),
            [("600000.SH", "20150101", ""), ("000001.SZ", "20150101", "")],
        )
        _write_snapshot(
            tmp_path,
            "000300.SH",
            date(2023, 1, 3),
            [
                ("600000.SH", "20150101", ""),
                ("000001.SZ", "20150101", ""),
                ("300750.SZ", "20230103", ""),
            ],
        )
        from mtzquant.data.calendar import TradeCalendar
        from mtzquant.data.drivers.csv_driver import CsvSourceDriver

        drv = CsvSourceDriver(root_path=str(tmp_path), kline_day_dir="kline/{type}/day")
        cal = TradeCalendar.from_dates(
            [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6), date(2023, 1, 3)]
        )
        return MarketDataProvider(drv, cal, fundamentals=FundamentalsStore(tmp_path))

    def test_index_stocks_uses_historical_snapshot(self, tmp_path: Path) -> None:
        prov = self._provider(tmp_path)
        # as_of=2020-06-01 → 只能看到 2020 快照（不含 2023 才加入的 300750.SZ）
        stocks = prov.index_stocks("000300.SH", _at(2020, 6, 1))
        assert "300750.SZ" not in stocks
        assert stocks == ["000001.SZ", "600000.SH"]
        # as_of=2024-01-01 → 最新快照含 300750.SZ（幸存者）
        stocks2 = prov.index_stocks("000300.SH", _at(2024, 1, 1))
        assert "300750.SZ" in stocks2

    def test_index_stocks_before_first_snapshot_errors(self, tmp_path: Path) -> None:
        prov = self._provider(tmp_path)
        with pytest.raises(MtzQuantError, match="成分快照"):
            prov.index_stocks("000300.SH", _at(2019, 1, 1))

    def test_provider_fundamentals_pit(self, tmp_path: Path) -> None:
        _write_fina(tmp_path, "600000.SH", [("20211028", "20210930", -5.0, 2.0)])
        from mtzquant.data.calendar import TradeCalendar
        from mtzquant.data.drivers.csv_driver import CsvSourceDriver

        drv = CsvSourceDriver(root_path=str(tmp_path), kline_day_dir="kline/{type}/day")
        cal = TradeCalendar.from_dates([date(2021, 10, 29)])
        prov = MarketDataProvider(drv, cal, fundamentals=FundamentalsStore(tmp_path))
        # 披露日后可见
        after = prov.fundamentals(
            "600000.SH", "fina_indicator", ["netprofit_yoy"], as_of=_at(2021, 11, 1)
        )
        assert len(after) == 1
        # 披露日前不可见（前视偏差）
        before = prov.fundamentals(
            "600000.SH", "fina_indicator", ["netprofit_yoy"], as_of=_at(2021, 10, 27)
        )
        assert before.empty
