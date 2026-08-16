# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 21:45:06
# @description : 证券代码归一化（设计 3.4）——纯函数，置于共享内核，适配器与数据层共同复用

"""证券代码归一化（设计 3.4）——纯函数，置于共享内核，适配器与数据层共同复用。

内部统一后缀: .SH(沪) / .SZ(深) / .BJ(北交所)；期货/期权预留 .CFE/.SHF/.DCE/.CZC/.INE。
归一化保证「策略里写的代码」与「数据文件名」两侧对齐（数据文件名一律 {code}.csv）。

支持输入形式（大小写不敏感、容忍首尾空白）:
    600000.XSHG     聚宽沪市      → 600000.SH
    000001.XSHE     聚宽深市      → 000001.SZ
    600000.SS       tushare/通用  → 600000.SH
    600000.SH/SZ/BJ 已归一（幂等）
    sh600000        前缀风格      → 600000.SH
    1.600000        QMT 市场号(1=沪, 0=深) → 600000.SH
    600000          裸 6 位（首位规则: 5/6/9→SH, 0/3→SZ, 4/8→BJ；1/2 有歧义须带后缀）
    IF2406          期货预留      → IF2406.CFE
"""

from __future__ import annotations

import re

from mtzquant.core.errors import InvalidCodeError

SH: str = ".SH"
SZ: str = ".SZ"
BJ: str = ".BJ"

# 后缀别名（设计 3.4 归一表 + 期货预留交易所）
_SUFFIX_ALIASES: dict[str, str] = {
    "XSHG": SH,
    "XSHE": SZ,
    "SS": SH,
    "SH": SH,
    "SZ": SZ,
    "BJ": BJ,
    "CFE": ".CFE",
    "SHF": ".SHF",
    "DCE": ".DCE",
    "CZC": ".CZC",
    "INE": ".INE",
}

# 前缀风格市场（sh/sz/bj + 6 位数字）
_PREFIX_MARKETS: dict[str, str] = {"SH": SH, "SZ": SZ, "BJ": BJ}

# QMT 市场号前缀（设计 3.4: `1.600000` → 600000.SH；0 = 深）
_QMT_MARKETS: dict[str, str] = {"1": SH, "0": SZ}

# 裸 6 位代码首位规则（设计附录 D: 6/9→沪, 0/3→深, 4/8→北交所；
# 5→沪市基金/ETF(510300)；1/2 存在沪深歧义，必须显式带后缀：159915.SZ）
_BARE_FIRST_DIGIT: dict[str, str] = {
    "5": SH,
    "6": SH,
    "9": SH,
    "0": SZ,
    "3": SZ,
    "4": BJ,
    "8": BJ,
}

# 期货品种前缀 → 交易所（预留，M5 随品种档案接入补齐全表）
_FUTURES_PREFIX: dict[str, str] = {"IF": ".CFE", "IH": ".CFE", "IC": ".CFE", "IM": ".CFE"}

_PREFIXED_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_QMT_RE = re.compile(r"^([01])\.(\d{6})$")
_BARE_DIGIT_RE = re.compile(r"^\d{6}$")
_BODY_RE = re.compile(r"^(\d{6}|[A-Z]{1,2}\d{3,4})$")
_FUTURES_RE = re.compile(r"^[A-Z]{1,2}\d{3,4}$")


def normalize_code(code: str) -> str:
    """将任意来源写法归一到内部统一代码（纯函数、幂等）。"""
    if not isinstance(code, str):
        raise InvalidCodeError(str(code), "输入必须是字符串")
    c = code.strip().upper()
    if not c:
        raise InvalidCodeError(code, "空字符串")

    # QMT 市场号风格（须在点分判断之前：`1.600000` 的点后段不是交易所后缀）
    m = _QMT_RE.match(c)
    if m:
        return f"{m.group(2)}{_QMT_MARKETS[m.group(1)]}"

    # 前缀风格: sh600000 / SZ000001
    m = _PREFIXED_RE.match(c)
    if m:
        return f"{m.group(2)}{_PREFIX_MARKETS[m.group(1)]}"

    # 点分后缀风格: 600000.XSHG / 600000.SS / 600000.SH(幂等)
    if "." in c:
        body, _, suffix = c.rpartition(".")
        alias = _SUFFIX_ALIASES.get(suffix)
        if alias is None:
            raise InvalidCodeError(code, f"不支持的交易所后缀 {suffix!r}")
        if not _BODY_RE.match(body):
            raise InvalidCodeError(code, f"代码主体格式非法: {body!r}")
        return f"{body}{alias}"

    # 裸 6 位数字: 按首位规则推断交易所
    if _BARE_DIGIT_RE.match(c):
        inferred = _BARE_FIRST_DIGIT.get(c[0])
        if inferred is not None:
            return f"{c}{inferred}"
        raise InvalidCodeError(
            code,
            "裸代码首位 1/2 存在沪深歧义（如 159915 为深市ETF、113050 为沪市转债），请显式带后缀",
        )

    # 期货合约（预留）: IF2406 → IF2406.CFE
    if _FUTURES_RE.match(c):
        fx_suffix = _FUTURES_PREFIX.get(c[:2])
        if fx_suffix is not None:
            return f"{c}{fx_suffix}"
        raise InvalidCodeError(code, "期货品种交易所映射未配置（M5 随品种档案接入）")

    raise InvalidCodeError(code, "无法匹配任何已知证券代码形式")


def exchange_of(code: str) -> str | None:
    """返回内部统一代码的交易所后缀（未带后缀时 None）。"""
    c = code.strip().upper()
    if c.endswith((".SH", ".SZ", ".BJ", ".CFE", ".SHF", ".DCE", ".CZC", ".INE")):
        return "." + c.rsplit(".", 1)[1]
    return None
