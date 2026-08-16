# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:56:00
# @update_time        : 2026/08/16 21:59:08
# @description : K7 兼容分级注册（设计 4.9）: API→L0/L1/L2 登记 + 未实现 API 结构化报错工厂

"""兼容分级与结构化报错（设计 4.9）。

- `register_api(platform, api, level)`: 把已实现/已登记 API 记入兼容清单（L0=官方语义全量/
  L1=常用子集/L2=尽力或占位）; `compat_report(platform)` 供 docs/compat/*.md 与评审使用。
- `not_implemented(platform, api, ...)`: 未实现 API 抛 `NotImplementedApiError`
  （带平台名/兼容清单路径/可选替代/实现指引, 4.9 模板）; 调用方（适配器）捕获后
  记入 run 的 error_log / degradation。
"""

from __future__ import annotations

from mtzquant.core.errors import NotImplementedApiError

# platform → {api: level}（L0/L1/L2, 4.9）
COMPAT_REGISTRY: dict[str, dict[str, str]] = {}


def register_api(platform: str, api: str, level: str = "L0") -> None:
    """登记 API 兼容级别（幂等; L0=全量语义, L1=常用子集, L2=尽力/占位）。"""
    COMPAT_REGISTRY.setdefault(platform, {})[api] = level


def compat_level(platform: str, api: str) -> str | None:
    """查询 API 兼容级别（未登记返回 None）。"""
    return COMPAT_REGISTRY.get(platform, {}).get(api)


def compat_report(platform: str) -> dict[str, str]:
    """某平台全部登记 API 及其级别（docs/compat 生成与 Q 验收对照用）。"""
    return dict(sorted(COMPAT_REGISTRY.get(platform, {}).items()))


def not_implemented(
    platform: str,
    api: str,
    *,
    level: str = "L2",
    alternative: str | None = None,
) -> NotImplementedApiError:
    """构造未实现 API 结构化异常（4.9 模板; 调用方捕获后记 error_log）。"""
    return NotImplementedApiError(api, platform, level=level, alternative=alternative)
