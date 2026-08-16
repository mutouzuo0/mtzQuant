# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:54:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D01：DriverRegistry + SourceDriver 协议签名（设计 3.2）

"""T-D01：DriverRegistry 注册/创建/未知报错 + SourceDriver 协议签名一致（设计 3.2）。"""

from __future__ import annotations

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import InstrumentType
from mtzquant.data.drivers.base import (
    DriverRegistry,
    InstrumentRef,
    SourceDriver,
    create_driver,
)
from mtzquant.data.drivers.csv_driver import CsvSourceDriver


def test_register_and_create() -> None:
    reg = DriverRegistry()
    reg.register("local_csv", CsvSourceDriver)
    inst = reg.create("local_csv", root_path=".")
    assert isinstance(inst, CsvSourceDriver)
    assert inst.name == "local_csv"


def test_register_name_must_be_nonempty() -> None:
    reg = DriverRegistry()
    with pytest.raises(MtzQuantError):
        reg.register("", CsvSourceDriver)


def test_duplicate_register_same_class_is_idempotent() -> None:
    reg = DriverRegistry()
    reg.register("x", CsvSourceDriver)
    reg.register("x", CsvSourceDriver)  # 同实现重注册不报错（幂等）


def test_duplicate_register_different_class_rejected() -> None:
    reg = DriverRegistry()

    class Other:  # 另一个“驱动”（仅占位类型）
        name = "other"

    reg.register("x", CsvSourceDriver)
    with pytest.raises(MtzQuantError):
        reg.register("x", Other)  # type: ignore[arg-type]


def test_unknown_driver_raises_with_known_list() -> None:
    reg = DriverRegistry()
    reg.register("local_csv", CsvSourceDriver)
    with pytest.raises(MtzQuantError) as exc:
        reg.create("nope")
    assert "未知数据驱动" in str(exc.value)
    assert "local_csv" in str(exc.value)  # 提示已注册驱动（AI 友好）


def test_default_registry_has_local_csv_registered() -> None:
    # 导入 mtzquant.data 包即注册（3.2: 新增数据源 = 一个模块 + 一行注册）
    inst = create_driver("local_csv", root_path=".")
    assert isinstance(inst, CsvSourceDriver)


def test_source_driver_protocol_signature_consistent() -> None:
    """CsvSourceDriver 必须满足 SourceDriver 协议（isinstance 运行时校验）。"""
    assert isinstance(CsvSourceDriver(root_path="."), SourceDriver)


def test_instrument_ref_fields() -> None:
    ref = InstrumentRef(code="510300.SH", instrument_type=InstrumentType.ETF, exchange="SH")
    assert ref.code == "510300.SH"
    assert ref.exchange == "SH"
