# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 21:45:06
# @description : 结构化异常体系（设计 10.5 / 4.9）：字段携带、机读输出、未实现 API 模板

"""结构化异常体系（设计 10.5 / 4.9）：字段携带、机读输出、未实现 API 模板。"""

from __future__ import annotations

from mtzquant.core.errors import (
    InvalidCodeError,
    MtzQuantError,
    NotImplementedApiError,
)


def test_mtzquant_error_carries_structured_fields() -> None:
    err = MtzQuantError("撮合失败", run_id="r_test", stage="broker.fill", hint="检查价格模型")
    assert err.run_id == "r_test"
    assert err.stage == "broker.fill"
    assert err.hint == "检查价格模型"
    # 字符串形式含全部结构信息（CLI 彩色输出时可读）
    text = str(err)
    assert "[mtzquant]" in text and "r_test" in text and "hint" not in text


def test_mtzquant_error_to_dict_is_machine_readable() -> None:
    info = MtzQuantError("x", stage="s").to_dict()
    assert set(info) == {"type", "message", "run_id", "stage", "hint"}
    assert info["type"] == "MtzQuantError"


def test_invalid_code_error_fields() -> None:
    err = InvalidCodeError("600000.US", "不支持的交易所后缀 'US'")
    info = err.to_dict()
    assert info["stage"] == "normalize_code"
    assert "600000.SH" in info["hint"]  # 列出期望形式（设计 3.5 AI 友好报错同款思路）


def test_not_implemented_api_error_template() -> None:
    """未实现 API 的报错必须含：平台、兼容清单、替代建议、实现指引（设计 4.9）。"""
    err = NotImplementedApiError(
        "get_extras", platform="joinquant", level="L2", alternative="get_price(fields=['paused'])"
    )
    text = str(err)
    assert "get_extras" in text and "L2" in text
    assert "docs/compat/joinquant.md" in text
    assert "get_price" in text  # 替代建议
    assert "适配器开发指南" in text  # 实现指引
    assert err.to_dict()["stage"] == "adapter:joinquant"
