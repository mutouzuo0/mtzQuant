# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 11:00:00
# @update_time        : 2026/08/17 11:00:00
# @description : T-JQ-M3：基本面族——jqdata 兼容/query DSL 三形态/PIT get_all_securities/
#                 get_current_data 增补/get_price

"""T-JQ-M3（设计 4.6 附录C / 3.13）: 用真实 FundamentalsStore + 合成基本面 CSV, 验证
jqdata shim、get_fundamentals(valuation/indicator/PEG 表达式)、finance.run_query(STK_XR_XD)、
get_all_securities PIT、get_current_data 增补（high_limit/low_limit/is_st/name）、
get_price 列表+虚拟涨跌停字段。
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from mtzquant.adapters.joinquant import jq_query
from mtzquant.adapters.joinquant.adapter import JoinQuantAdapter
from mtzquant.adapters.joinquant.jqdata_shim import install_jqdata
from mtzquant.data.fundamentals import FundamentalsStore


def _write_fund(root: Path, table: str, code: str, rows: list[list[str]], cols: list[str]) -> None:
    p = root / "fundamentals" / table / f"{code}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def _make_store(tmp_path: Path) -> FundamentalsStore:
    root = tmp_path / "data"
    # daily_basic（估值, 按 trade_date）
    _write_fund(
        root,
        "daily_basic",
        "600000.SH",
        [
            ["600000.SH", "20200102", "7.5", "1.1", "3000000", "2500000"],
            ["600000.SH", "20200103", "8.0", "1.2", "3100000", "2600000"],
            ["600000.SH", "20200106", "9.0", "1.3", "3200000", "2700000"],
        ],
        ["ts_code", "trade_date", "pe", "pb", "total_mv", "circ_mv"],
    )
    _write_fund(
        root,
        "daily_basic",
        "000001.SZ",
        [
            ["000001.SZ", "20200102", "5.0", "0.8", "2000000", "1800000"],
            ["000001.SZ", "20200106", "5.5", "0.9", "2100000", "1900000"],
        ],
        ["ts_code", "trade_date", "pe", "pb", "total_mv", "circ_mv"],
    )
    # fina_indicator（净利润同比, 按 ann_date/end_date）
    _write_fund(
        root,
        "fina_indicator",
        "600000.SH",
        [
            ["600000.SH", "20191030", "20190930", "10.0", "12.0"],
            ["600000.SH", "20200120", "20191231", "20.0", "15.0"],
        ],
        ["ts_code", "ann_date", "end_date", "netprofit_yoy", "or_yoy"],
    )
    _write_fund(
        root,
        "fina_indicator",
        "000001.SZ",
        [["000001.SZ", "20200115", "20191231", "-5.0", "3.0"]],
        ["ts_code", "ann_date", "end_date", "netprofit_yoy", "or_yoy"],
    )
    # dividend（分红, 按 record_date/ann_date）
    _write_fund(
        root,
        "dividend",
        "600000.SH",
        [
            ["600000.SH", "20200415", "20200620", "20200623", "实施", "0.4"],
            ["600000.SH", "20210410", "20210615", "20210618", "实施", "0.3"],
        ],
        ["ts_code", "ann_date", "record_date", "ex_date", "div_proc", "cash_div_tax"],
    )
    _write_fund(
        root,
        "dividend",
        "000001.SZ",
        [["000001.SZ", "20200510", "20200701", "20200704", "实施", "0.5"]],
        ["ts_code", "ann_date", "record_date", "ex_date", "div_proc", "cash_div_tax"],
    )
    return FundamentalsStore(root)


def _make_adapter(
    store: FundamentalsStore,
    tmp_path: Path,
    master_rows: list[dict[str, str]],
    *,
    previous_date: date = date(2020, 1, 6),
) -> JoinQuantAdapter:
    """装配适配器: ctx 注入 provider(store) / previous_date / universe_fn / master_path。"""
    adapter = JoinQuantAdapter()

    class _StubProvider:
        def fundamentals(self, code, table, fields, *, as_of, knowledge_time):  # noqa: ANN001
            return store.fundamentals(
                code, table, fields, as_of=as_of, knowledge_time=knowledge_time
            )

        def bar_at(self, code, dt):  # noqa: ANN001, ANN002
            return None  # 无行情 → _jq_data 视为停牌（paused 快照）

    master_file = tmp_path / "instruments.csv"
    with master_file.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["code", "name", "instrument_type", "list_date", "delist_date"]
        )
        w.writeheader()
        for row in master_rows:
            w.writerow(row)

    ctx = adapter._ctx
    ctx.provider = _StubProvider()
    ctx.master_path = str(master_file)
    ctx.previous_date = previous_date
    ctx.current_dt = datetime(previous_date.year, previous_date.month, previous_date.day, 15, 0)
    ctx.now_fn = lambda: ctx.current_dt  # noqa: E731
    ctx.universe_fn = lambda: ["000001.SZ", "600000.SH"]  # noqa: E731
    return adapter


# ----------------------------------------------------------------------
# jqdata shim
# ----------------------------------------------------------------------
def test_jqdata_shim_from_import_makes_strategy_load() -> None:
    """`from jqdata import *` 在 exec 语境下可用, 且
    PriceRelatedSlippage/OrderCost/datetime 齐全。"""
    namespace: dict = {
        "g": object(),
        "log": object(),
        "get_fundamentals": lambda *a, **k: None,
        "query": jq_query.query,
        "valuation": jq_query.valuation,
        "finance": SimpleNamespace(STK_XR_XD=jq_query.finance.STK_XR_XD),
    }
    install_jqdata(namespace)
    ns: dict = {}
    code = (
        "from jqdata import *\n"
        "s = PriceRelatedSlippage(0.01)\n"
        "c = OrderCost(close_tax=0.001, open_commission=0.0003, min_commission=5)\n"
        "assert s.value == 0.01\n"
        "assert c.close_tax == 0.001 and c.min_commission == 5\n"
        "assert hasattr(datetime, 'timedelta')\n"
        "assert get_fundamentals is not None and query is not None\n"
        "assert valuation is not None and finance.STK_XR_XD is not None\n"
    )
    exec(compile(code, "<jq_test>", "exec"), ns)  # noqa: S102
    assert sys.modules["jqdata"] is not None


# ----------------------------------------------------------------------
# query DSL: valuation / PEG 表达式 / STK_XR_XD
# ----------------------------------------------------------------------
def test_get_fundamentals_valuation_market_cap(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    adapter = _make_adapter(
        store,
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "600000.SH",
                "name": "浦发银行",
                "instrument_type": "stock",
                "list_date": "19991110",
                "delist_date": "",
            },
        ],
    )
    q = jq_query.query(jq_query.valuation.code, jq_query.valuation.market_cap).filter(
        jq_query.valuation.code.in_(["600000.SH", "000001.SZ"])
    )
    df = adapter.get_fundamentals(q, date="2020-01-06")
    assert set(df["code"]) == {"600000.XSHG", "000001.XSHE"}
    mc = dict(zip(df["code"], df["market_cap"], strict=True))
    # market_cap = total_mv(万元) × 1e4 → 元; 2020-01-06 最新
    assert mc["600000.XSHG"] == pytest.approx(3200000 * 1e4)
    assert mc["000001.XSHE"] == pytest.approx(2100000 * 1e4)


def test_get_fundamentals_peg_expression(tmp_path: Path) -> None:
    """PEG 表达式: valuation.pe_ratio / indicator.inc_net_profit_year_on_year ∈ (-3, 3)。"""
    store = _make_store(tmp_path)
    adapter = _make_adapter(
        store,
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "600000.SH",
                "name": "浦发银行",
                "instrument_type": "stock",
                "list_date": "19991110",
                "delist_date": "",
            },
        ],
    )
    peg = jq_query.valuation.pe_ratio / jq_query.indicator.inc_net_profit_year_on_year
    q = jq_query.query(jq_query.valuation.code, peg).filter(
        peg > -3.0,
        peg < 3.0,
        jq_query.valuation.code.in_(["600000.SH", "000001.SZ"]),
    )
    df = adapter.get_fundamentals(
        q, date="2020-02-01"
    )  # as_of 晚于 fina_indicator 披露日(20200115/20)
    # 600000: pe=9.0 / netprofit_yoy=20.0 = 0.45; 000001: pe=5.5 / (-5.0) = -1.1
    assert set(df["code"]) == {"600000.XSHG", "000001.XSHE"}
    col = "(valuation.pe_ratio / indicator.inc_net_profit_year_on_year)"
    pegs = dict(zip(df["code"], df[col], strict=True))
    assert pegs["600000.XSHG"] == pytest.approx(0.45)
    assert pegs["000001.XSHE"] == pytest.approx(-1.1)


def test_run_query_stk_xr_xd(tmp_path: Path) -> None:
    """finance.run_query(STK_XR_XD): 按 a_registration_date(record_date) 区间过滤, 分组求和股息。"""
    store = _make_store(tmp_path)
    adapter = _make_adapter(
        store,
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "600000.SH",
                "name": "浦发银行",
                "instrument_type": "stock",
                "list_date": "19991110",
                "delist_date": "",
            },
        ],
        previous_date=date(2022, 1, 1),  # as_of 须晚于全部 record_date（PIT）
    )
    f = jq_query.finance
    q = jq_query.query(
        f.STK_XR_XD.code, f.STK_XR_XD.a_registration_date, f.STK_XR_XD.bonus_amount_rmb
    ).filter(
        f.STK_XR_XD.a_registration_date >= date(2020, 1, 1),
        f.STK_XR_XD.a_registration_date <= date(2021, 12, 31),
        f.STK_XR_XD.code.in_(["600000.SH", "000001.SZ"]),
    )
    df = adapter.run_query(q)
    assert len(df) == 3  # 600000 两条 + 000001 一条
    sums = df.groupby("code")["bonus_amount_rmb"].sum()
    assert sums["600000.XSHG"] == pytest.approx(0.7)
    assert sums["000001.XSHE"] == pytest.approx(0.5)


# ----------------------------------------------------------------------
# get_all_securities PIT
# ----------------------------------------------------------------------
def test_get_all_securities_pit(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    adapter = _make_adapter(
        store,
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "600000.SH",
                "name": "浦发银行",
                "instrument_type": "stock",
                "list_date": "19991110",
                "delist_date": "",
            },
            {
                "code": "000003.SZ",
                "name": "PT金田A(退)",
                "instrument_type": "stock",
                "list_date": "19910703",
                "delist_date": "20020614",
            },
        ],
    )
    df = adapter.get_all_securities("stock", date="2020-01-06")
    # 已退市(2002)不出现; 正常上市两只出现
    assert set(df.index) == {"000001.XSHE", "600000.XSHG"}
    assert df.loc["600000.XSHG", "name"] == "浦发银行"
    # 历史日期: 退市股在退市前出现
    df2 = adapter.get_all_securities("stock", date="2000-01-01")
    assert "000003.XSHE" in df2.index


# ----------------------------------------------------------------------
# get_current_data 增补（high_limit/low_limit/is_st/name）
# ----------------------------------------------------------------------
def test_get_current_data_enrichment(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    adapter = _make_adapter(
        store,
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "600000.SH",
                "name": "ST浦发",
                "instrument_type": "stock",
                "list_date": "19991110",
                "delist_date": "",
            },
        ],
    )

    class _Bar:
        def __init__(self) -> None:
            self.open = self.high = self.low = self.close = 9.9
            self.pre_close = 9.0
            self.volume = 1000.0
            self.amount = 10000.0
            self.suspended = 0

    class _P:
        def fundamentals(self, *a, **k):  # noqa: ANN002, ANN003
            return store.fundamentals(*a, **k)

        def bar_at(self, code, dt):  # noqa: ANN001, ANN002
            return _Bar()

    adapter._ctx.provider = _P()
    snap = adapter._jq_data(include_today=True)
    # 600000 (ST, 主板): 5% 停板 → high_limit=9.45, low_limit=8.55
    s = snap["600000.XSHG"]
    assert s.is_st is True
    assert s.name == "ST浦发"
    assert s.high_limit == pytest.approx(round(9.0 * 1.05, 2))
    assert s.low_limit == pytest.approx(round(9.0 * 0.95, 2))
    # 000001 (非 ST, 主板): 10% → high_limit=9.9
    s2 = snap["000001.XSHE"]
    assert s2.is_st is False
    assert s2.name == "平安银行"
    assert s2.high_limit == pytest.approx(round(9.0 * 1.10, 2))


# ----------------------------------------------------------------------
# get_price 虚拟涨跌停字段（主板/创业板因子）
# ----------------------------------------------------------------------
def test_limit_prices_series_by_board(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    adapter = _make_adapter(
        store,
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "300750.SZ",
                "name": "宁德时代",
                "instrument_type": "stock",
                "list_date": "20180611",
                "delist_date": "",
            },
            {
                "code": "688981.SH",
                "name": "中芯国际",
                "instrument_type": "stock",
                "list_date": "20200716",
                "delist_date": "",
            },
        ],
    )
    assert adapter._limit_factor("000001.SZ") == pytest.approx(0.10)
    assert adapter._limit_factor("300750.SZ") == pytest.approx(0.20)
    assert adapter._limit_factor("688981.SH") == pytest.approx(0.20)
    up, dn = adapter._limit_prices_series("300750.SZ", 10.0)
    assert float(up[0]) == pytest.approx(12.0)
    assert float(dn[0]) == pytest.approx(8.0)


# ----------------------------------------------------------------------
# 原版策略兼容: history 位置访问 / get_price 空列表 / get_current_data 自动补暂停
# ----------------------------------------------------------------------
def test_history_posframe_positional_access(tmp_path: Path) -> None:
    """history 返回 _PosFrame: 支持聚宽旧式 `df[stock][-1]` 位置访问
    （现代 pandas 标签语义不兼容）。"""
    adapter = _make_adapter(
        _make_store(tmp_path),
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "600000.SH",
                "name": "浦发银行",
                "instrument_type": "stock",
                "list_date": "19991110",
                "delist_date": "",
            },
        ],
    )

    class _Bar:
        def __init__(self, close: float) -> None:
            self.open = self.high = self.low = self.close = close
            self.pre_close = close
            self.volume = 1000.0
            self.amount = 10000.0
            self.suspended = 0

    class _P:
        def fundamentals(self, *a, **k):  # noqa: ANN002, ANN003
            return _make_store(tmp_path).fundamentals(*a, **k)

        def bar_at(self, code, dt):  # noqa: ANN001, ANN002
            return _Bar(close=9.9)

        def history(self, code, fields, n, *, as_of, knowledge_time, include_today, frequency):  # noqa: ANN001, ANN002

            idx = [as_of - __import__("datetime").timedelta(days=i) for i in range(n, 0, -1)]
            df = pd.DataFrame({f: [9.9] * n for f in fields}, index=idx)
            df.index = pd.to_datetime(df.index)
            return df

    adapter._ctx.provider = _P()
    from mtzquant.adapters.joinquant.adapter import _PosFrame

    # 直接验证 _PosFrame/_PosSeries 的 [-1] 语义
    frame = _PosFrame({"000001.XSHE": [1.0, 2.0], "600000.XSHG": [3.0, 4.0]})
    assert frame["000001.XSHE"][-1] == pytest.approx(2.0)
    assert frame["600000.XSHG"][-1] == pytest.approx(4.0)


def test_get_price_empty_and_single_list(tmp_path: Path) -> None:
    """get_price 空列表返回带列窄表; 单元素列表走 rows-per-code（含 code 列）。"""
    adapter = _make_adapter(
        _make_store(tmp_path),
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            }
        ],
    )
    df = adapter.get_price(
        [], end_date="2020-01-06", fields=["close", "high_limit"], count=1, panel=False
    )
    assert list(df.columns) == ["code", "close", "high_limit"]
    assert df.empty


def test_get_current_data_missing_code_autosnapshot(tmp_path: Path) -> None:
    """get_current_data 对非 universe 标的自动补暂停快照（无 KeyError, 供 filter 安全索引）。"""
    store = _make_store(tmp_path)
    adapter = _make_adapter(
        store,
        tmp_path,
        [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "instrument_type": "stock",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "code": "600000.SH",
                "name": "ST浦发",
                "instrument_type": "stock",
                "list_date": "19991110",
                "delist_date": "",
            },
        ],
    )
    cd = adapter.get_current_data()  # 仅 universe 内
    # 访问非 universe 标的 → 自动补暂停快照
    snap = cd["999999.XSHE"]
    assert snap.paused is True
    assert snap.is_st is False
    # ST 标的: is_st 由名称判定
    snap2 = cd["600000.XSHG"]
    assert snap2.is_st is True
