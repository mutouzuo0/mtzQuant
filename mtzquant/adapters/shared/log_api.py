# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:44:00
# @update_time        : 2026/08/16 09:44:00
# @description : K2 log API（设计 4.4/6.2）：info/warn/warning/error → ResultStore log 事件

"""log API（设计 4.4/6.2）——`log.info/warn/warning/error` → `ResultStore` log 事件。

两平台方法名双拼写兼容（聚宽 `log.warn` / PTrade `log.warning`, 两者都提供）。
事件 payload 带 level/当前回测时刻（current_dt, 便于按日检索日志）。
`emit` 由 adapter 注入（绑定 session.emit, 6.3 信封源）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any


def _level_fn(
    emit: Callable[[str, dict[str, Any]], None],
    level: str,
    current_dt: Callable[[], datetime] | None,
) -> Callable[..., None]:
    def _log(message: Any, *args: Any, **kw: Any) -> None:
        text = message if isinstance(message, str) else str(message)
        if args:  # printf 风格（含 %s）或空格拼接（多参数日志兼容, 官方两种用法都有）
            if "%" in text:
                try:
                    text = text % args
                except (TypeError, ValueError):
                    text = " ".join([text] + [str(a) for a in args])
            else:
                text = " ".join([text] + [str(a) for a in args])
        payload: dict[str, Any] = {"level": level, "message": text}
        if current_dt is not None:
            try:
                payload["current_dt"] = current_dt().isoformat()
            except (TypeError, ValueError):
                pass
        if kw:
            payload["extra"] = dict(kw)
        emit("log", payload)

    return _log


def make_log(
    emit: Callable[[str, dict[str, Any]], None],
    *,
    current_dt: Callable[[], datetime] | None = None,
) -> SimpleNamespace:
    """构造策略可见 `log` 对象（双拼写兼容, 4.4; current_dt 可选带回测内时刻）。"""
    log = SimpleNamespace()
    log.info = _level_fn(emit, "info", current_dt)  # type: ignore[attr-defined]
    log.warn = _level_fn(emit, "warn", current_dt)  # type: ignore[attr-defined]  # 聚宽拼写
    log.warning = _level_fn(emit, "warning", current_dt)  # type: ignore[attr-defined]  # PTrade 拼写
    log.error = _level_fn(emit, "error", current_dt)  # type: ignore[attr-defined]
    return log
