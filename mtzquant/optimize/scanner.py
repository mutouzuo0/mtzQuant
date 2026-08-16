# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:40:00
# @update_time        : 2026/08/17 00:40:00
# @description : M3-T1 ParamOptimizer：参数空间展开（grid/random 确定性, 设计 10.4）

"""参数优化器（设计 10.4, M3-T1）——扫描任务流生成。

- `search(base_task, space, mode)`:
    grid    空间直积展开（全组合）;
    random  seed=42 播种随机采样 n_trials（确定性: 同 space 两次随机扫描任务序列全同, T-O01）;
- 每任务 = base_task 深拷贝 + `task.engine.params` 参数注入（策略在 initialize 后经
  session 注入覆盖默认 g 值, M3-T1 框架钩子）;

边界: 只生成任务流, 不执行——执行与汇聚归 BacktestQueue（T2）。bayesian 预留 mode（§2.2）。
"""

from __future__ import annotations

import copy
import itertools
import random
from collections.abc import Iterator
from typing import Any

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.manifest import RANDOM_SEED

MODES = ("grid", "random")


class ParamOptimizer:
    """参数空间扫描器（10.4）。"""

    def __init__(self, *, seed: int = RANDOM_SEED) -> None:
        self.seed = seed

    # ------------------------------------------------------------------
    def search(
        self,
        base_task: dict[str, Any],
        space: dict[str, list[Any]],
        mode: str = "grid",
        n_trials: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """展开扫描任务流（每任务含注入参数）。"""
        if mode not in MODES:
            raise MtzQuantError(
                f"未知扫描模式 {mode!r}", stage="optimizer", hint=f"可选: {MODES}（bayesian 预留）"
            )
        if not space:
            raise MtzQuantError(
                "space 为空", stage="optimizer", hint='--space \'{"fast":[5,10],"slow":[20,40]}\''
            )
        combos = self._combos(space, mode, n_trials)
        return (self._inject_params(base_task, combo) for combo in combos)

    def _combos(
        self, space: dict[str, list[Any]], mode: str, n_trials: int | None
    ) -> list[dict[str, Any]]:
        keys = sorted(space)
        if mode == "grid":
            return [
                dict(zip(keys, values, strict=True))
                for values in itertools.product(*(space[k] for k in keys))
            ]
        # random: seed=42 播种, 确定性（8.8 禁未播种随机）
        n = n_trials if n_trials is not None else _default_trials(space)
        rng = random.Random(self.seed)
        combos: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        while len(combos) < n:
            combo = tuple(rng.choice(space[k]) for k in keys)
            if combo in seen:
                continue
            seen.add(combo)
            combos.append(dict(zip(keys, combo, strict=True)))
        return combos

    # ------------------------------------------------------------------
    @staticmethod
    def _inject_params(base_task: dict[str, Any], combo: dict[str, Any]) -> dict[str, Any]:
        task = copy.deepcopy(base_task)
        engine = dict(task.get("engine") or {})
        engine["params"] = dict(combo)
        task["engine"] = engine
        return task


def _default_trials(space: dict[str, list[Any]]) -> int:
    """random 缺省试次数 = min(直积规模, 32)（防空间爆炸, 10.4）。"""
    total = 1
    for v in space.values():
        total *= max(len(v), 1)
    return min(max(total, 1), 32)
