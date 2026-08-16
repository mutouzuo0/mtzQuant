# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:42:00
# @update_time        : 2026/08/16 21:59:08
# @description : K1 g 动态属性容器（设计 4.4）：属性/下标双风格, 未定义访问给结构化提示

"""GContainer（设计 4.4）——`g` 会话级容器。

双风格兼容:
  - 属性风格: `g.foo = 1` / `g.foo`（PTrade / 聚宽原生写法）
  - 下标风格: `g["foo"] = 1` / `g["foo"]`（native 黄金用例既有写法, 8.8 确定性计数）

会话生命周期存活（adapter 持有同一实例）; 未定义属性/键访问抛 MtzQuantError
（区别于 Python 裸 AttributeError, 便于结构化报错与 AI 解析, 4.9）。
"""

from __future__ import annotations

from typing import Any

from mtzquant.core.errors import MtzQuantError

_EMPTY = object()


class GContainer:
    """g 容器（dict 兜底 + 属性/下标双访问器）。"""

    def __init__(self) -> None:
        object.__setattr__(self, "_data", {})

    # ------------------------------------------------------------------
    # 属性风格
    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if name in data:
            return data[name]
        if name.startswith("_"):
            raise AttributeError(name)
        raise MtzQuantError(
            f"g 容器无属性 {name!r}（未在 initialize 中赋值）",
            stage="adapter:g",
            hint=f"g 容器现有属性: {', '.join(sorted(data)) or '（空）'}; 请先 g.{name} = ...",
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    def __delattr__(self, name: str) -> None:
        if name in self._data:
            del self._data[name]
            return
        raise MtzQuantError(f"g 容器无属性可删: {name!r}", stage="adapter:g")

    # ------------------------------------------------------------------
    # 下标风格
    # ------------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        if key in self._data:
            return self._data[key]
        raise MtzQuantError(
            f"g 容器无键 {key!r}", stage="adapter:g", hint="请先 g[key] = ... 或 g.key = ..."
        )

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def items(self):
        return self._data.items()

    def __repr__(self) -> str:
        return f"GContainer({self._data!r})"
