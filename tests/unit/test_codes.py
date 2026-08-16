# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 21:45:06
# @description : T-U01：证券代码归一化（设计 3.4）——表驱动全映射 + 幂等 + 非法输入

"""T-U01：证券代码归一化（设计 3.4）——表驱动全映射 + 幂等 + 非法输入。"""

from __future__ import annotations

import pytest

from mtzquant.core.codes import exchange_of, normalize_code
from mtzquant.core.errors import InvalidCodeError

# (输入, 期望归一结果) —— 覆盖设计 3.4 归一表的全部来源写法
CASES: list[tuple[str, str]] = [
    ("600000.XSHG", "600000.SH"),  # 聚宽沪市
    ("000001.XSHE", "000001.SZ"),  # 聚宽深市
    ("600000.SS", "600000.SH"),  # tushare/通用 沪
    ("510300.SH", "510300.SH"),  # 已归一（幂等路径）
    ("sh600000", "600000.SH"),  # 前缀风格 沪
    ("SZ000001", "000001.SZ"),  # 前缀风格 深
    ("bj430047", "430047.BJ"),  # 前缀风格 北交所
    ("1.600000", "600000.SH"),  # QMT 市场号 沪
    ("0.000001", "000001.SZ"),  # QMT 市场号 深
    ("600000", "600000.SH"),  # 裸 6 位：首位 6 → 沪
    ("000001", "000001.SZ"),  # 裸 6 位：首位 0 → 深
    ("510300", "510300.SH"),  # 裸 6 位：首位 5 → 沪（ETF）
    ("900905", "900905.SH"),  # 裸 6 位：首位 9 → 沪
    ("300750", "300750.SZ"),  # 裸 6 位：首位 3 → 深（创业板）
    ("830799", "830799.BJ"),  # 裸 6 位：首位 8 → 北交所
    ("159915.SZ", "159915.SZ"),  # 深市 ETF 显式后缀
    ("600000.xshg", "600000.SH"),  # 小写后缀
    ("  600000.SH  ", "600000.SH"),  # 首尾空白容忍
    ("IF2406", "IF2406.CFE"),  # 期货预留（设计 3.4）
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize_code(raw: str, expected: str) -> None:
    assert normalize_code(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize_code_is_idempotent(raw: str, expected: str) -> None:
    once = normalize_code(raw)
    assert once == expected
    # 归一结果再次归一必须不变（数据文件名与内存键两侧对齐的保证）
    assert normalize_code(once) == once


# 非法输入：全部应抛 InvalidCodeError（带 stage 与建议，非裸 Exception）
INVALID: list[str | None] = [
    "",
    "  ",
    "abc",
    "600000.US",  # 未知交易所后缀
    "600000.",  # 空后缀
    ".SH",  # 空主体
    "abc.XSHG",  # 主体格式非法
    "159915",  # 首位 1：沪深歧义（159915=深市ETF，113050=沪市转债）
    "113050",
    "1.60000",  # QMT 格式位数不足
    "rb2410",  # 期货品种映射未配置（M5 预留）
    None,  # 非字符串
]


@pytest.mark.parametrize("raw", INVALID)
def test_invalid_code_raises_structured_error(raw: str | None) -> None:
    with pytest.raises(InvalidCodeError) as exc_info:
        normalize_code(raw)  # type: ignore[arg-type]
    info = exc_info.value.to_dict()
    assert info["type"] == "InvalidCodeError"
    assert info["stage"] == "normalize_code"
    assert info["hint"]  # 修复建议非空（AI 友好，设计 10.5）


def test_exchange_of() -> None:
    assert exchange_of("600000.SH") == ".SH"
    assert exchange_of("000001.SZ") == ".SZ"
    assert exchange_of("430047.BJ") == ".BJ"
    assert exchange_of("IF2406.CFE") == ".CFE"
    assert exchange_of("600000") is None  # 未带后缀
