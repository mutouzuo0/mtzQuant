# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 01:22:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D07：主数据 读写/upsert/快照（设计 3.11）

"""T-D07：master/instruments.csv 读写、upsert 语义、快照留档、脏值清洗（设计 3.11）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from mtzquant.data.master import InstrumentRow, MasterStore, clean_field


def _store(tmp_path: Path) -> MasterStore:  # type: ignore[no-untyped-def]
    return MasterStore(tmp_path / "master" / "instruments.csv")


def _row(code: str, **kw) -> InstrumentRow:  # type: ignore[no-untyped-def]
    defaults = dict(
        name="初始名", instrument_type="etf", exchange="SH", list_date="2020-06-01", delist_date=""
    )
    defaults.update(kw)
    return InstrumentRow(code=code, **defaults)


def test_read_missing_returns_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    df = store.read()
    assert df.empty
    assert "code" in df.index.names or df.index.name == "code"


def test_upsert_append_new(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    added, updated = store.upsert([_row("510300.SH"), _row("510500.SH", name="500ETF")])
    assert (added, updated) == (2, 0)
    df = store.read()
    assert set(df.index) == {"510300.SH", "510500.SH"}
    assert df.loc["510500.SH", "name"] == "500ETF"


def test_upsert_update_mutable_keep_list_date(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """已有行: 更新可变字段, 保留首次 list_date（3.11）。"""
    store = _store(tmp_path)
    store.upsert([_row("510300.SH", list_date="2020-06-01")])
    added, updated = store.upsert(
        [_row("510300.SH", name="改名后", list_date="2099-01-01", industry="金融")]
    )
    assert (added, updated) == (0, 1)
    row = store.get("510300.SH")
    assert row is not None
    assert row.name == "改名后"  # 可变字段更新
    assert row.list_date == "2020-06-01"  # 首次 list_date 保留
    assert row.industry == "金融"


def test_upsert_idempotent_second_same(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    store.upsert([_row("510300.SH", name="A")])
    store.upsert([_row("510300.SH", name="A")])
    df = store.read()
    assert len(df) == 1  # 幂等: 不产生重复行
    assert df.loc["510300.SH", "name"] == "A"


def test_get_missing_returns_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    assert store.get("600000.SH") is None
    assert store.get_name("600000.SH") == ""


def test_snapshot_archived_on_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    store.upsert([_row("510300.SH")])
    snaps = list((tmp_path / "master" / "snapshots").glob("instruments_*.csv"))
    assert len(snaps) == 1
    df = pd.read_csv(snaps[0])
    assert "510300.SH" in df["code"].tolist()


def test_clean_field_dirty_values() -> None:
    assert clean_field("99999999") == ""
    assert clean_field("00000000") == ""
    assert clean_field("") == ""
    assert clean_field("nan") == ""
    assert clean_field("2024-01-02") == "2024-01-02"


def test_roundtrip_preserves_all_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    store.upsert(
        [_row("600000.SH", instrument_type="stock", underlying="", delist_date="2025-06-30")]
    )
    row = store.get("600000.SH")
    assert row is not None
    assert row.instrument_type == "stock"
    assert row.delist_date == "2025-06-30"
