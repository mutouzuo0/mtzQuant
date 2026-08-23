# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:54:00
# @update_time        : 2026/08/23 10:20:00
# @description : K8 平台代码风格互转（设计 3.4/4.7）: 聚宽 .XSHG/.XSHE, PTrade .SS/.SZ

"""平台代码风格互转（设计 3.4/4.7）。

- 入参归一复用 `mtzquant.core.codes.normalize_code`（不复制实现, 单一事实源）;
- 平台对外输出: 聚宽 `600000.XSHG / 000001.XSHE`; **PTrade 真实外部码为
  `600000.SS / 000001.SZ`**（恒生/华泰 PTrade 约定, 4.7 修订）——走 `platform="ptrade"`;
- 北交所 `.BJ` 无平台别名, 原样透传（compat 文档登记为已知近似）。
"""

from __future__ import annotations

from mtzquant.core.codes import normalize_code

# 内部后缀 → 平台外部后缀（聚宽, 4.7）
_PLATFORM_SUFFIX: dict[str, str] = {
    ".SH": ".XSHG",
    ".SZ": ".XSHE",
    ".BJ": ".BJ",  # 北交所无官方别名, 透传
    ".CFE": ".CFE",
    ".SHF": ".SHF",
    ".DCE": ".DCE",
    ".CZC": ".CZC",
    ".INE": ".INE",
}

# 内部后缀 → PTrade 真实外部后缀（恒生/华泰 PTrade: 沪市 .SS, 深市 .SZ, 4.7 修订）
_PTRADE_SUFFIX: dict[str, str] = {
    ".SH": ".SS",
    ".SZ": ".SZ",
    ".BJ": ".BJ",
    ".CFE": ".CFE",
    ".SHF": ".SHF",
    ".DCE": ".DCE",
    ".CZC": ".CZC",
    ".INE": ".INE",
}


def denormalize_code(code: str, platform: str = "") -> str:
    """内部码 → 平台外部码（600000.SH → 600000.XSHG; ptrade 下 600000.SS; 幂等）。

    `platform="ptrade"` 走 PTrade 真实外部码（.SS/.SZ, 4.7 修订）, 缺省按聚宽 .XSHG/.XSHE。
    """
    norm = normalize_code(code)
    table = _PTRADE_SUFFIX if platform == "ptrade" else _PLATFORM_SUFFIX
    suffix = norm[-4:]
    if suffix in table:
        return norm[:-4] + table[suffix]
    # 六位+3字符后缀（.SH/.SZ/.BJ）
    suffix3 = norm[-3:]
    if suffix3 in table:
        return norm[:-3] + table[suffix3]
    return norm


def round_trip(code: str) -> str:
    """往返校验: 平台码 → 归一 → 平台码（T-A08 断言用）。"""
    return denormalize_code(normalize_code(code))
