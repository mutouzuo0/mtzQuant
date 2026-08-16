# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 01:25:00
# @update_time        : 2026/08/17 01:25:00
# @description : M3-U5 多重检验接口：Bonferroni/BH 校正（5.8.2, selection_count 供其消费）

"""多重检验校正（设计 5.8.2, M3-U5）——参数扫描 multiple-testing 修正接口。

- `bonferroni(p_values, alpha)`: 最保守（FWER）——p ≤ alpha/k 才显著;
- `benjamini_hochberg(p_values, alpha)`: 控制 FDR（扫描多参数下的主流口径）;

`selection_count`（该参数共尝试多少组, T3 已入库）供消费: 校正时 k 可设为
selection_count（尝试组数越多, 阈值越严——防过拟合, 5.8.2）。最小实现, Research Pro 可校验。
"""

from __future__ import annotations

from collections.abc import Sequence

from mtzquant.core.errors import MtzQuantError


def bonferroni(p_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Bonferroni 校正（FWER）: 拒绝 p_i <= alpha/k（k=检验数）。"""
    if not 0 < alpha < 1:
        raise MtzQuantError(f"alpha 须在 (0,1), 得到 {alpha}", stage="multipletest")
    p = _validate(p_values)
    k = len(p)
    if k == 0:
        return []
    return [x <= alpha / k for x in p]


def benjamini_hochberg(p_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg 校正（FDR）: 升序 p, 找到最大 i 使 p_(i) <= alpha*i/k。"""
    if not 0 < alpha < 1:
        raise MtzQuantError(f"alpha 须在 (0,1), 得到 {alpha}", stage="multipletest")
    p = _validate(p_values)
    k = len(p)
    if k == 0:
        return []
    order = sorted(range(k), key=lambda i: p[i])
    rejected = [False] * k
    for rank, i in enumerate(order, start=1):
        th = alpha * rank / k
        if p[i] <= th:
            rejected[i] = True
    return rejected


def adjusted_p_bonferroni(p_values: Sequence[float]) -> list[float]:
    """Bonferroni 校正 p 值（min(1, p*k)）。"""
    p = _validate(p_values)
    k = len(p)
    if k == 0:
        return []
    return [min(1.0, x * k) for x in p]


def _validate(p_values: Sequence[float]) -> list[float]:
    p = [float(x) for x in p_values]
    if any(x < 0 or x > 1 for x in p):
        raise MtzQuantError("p 值须在 [0,1]", stage="multipletest")
    return p
