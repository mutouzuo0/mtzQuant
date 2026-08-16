# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:09:30
# @description : T-U03：点时查询协议（设计 3.13）——双时间过滤与防未来数据

"""T-U03：点时查询协议（设计 3.13）——双时间过滤与防未来数据。"""

from __future__ import annotations

from datetime import datetime

from mtzquant.core.pit import PitQuery


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s + "+08:00")


def test_knowledge_time_defaults_to_as_of() -> None:
    as_of = _dt("2024-01-02T15:00")
    q = PitQuery(as_of=as_of)
    assert q.knowledge_time == as_of  # 行情类单参即可


def test_event_visible_boundaries() -> None:
    as_of = _dt("2024-01-02T15:00")
    q = PitQuery(as_of=as_of)
    assert q.event_visible(_dt("2024-01-02T15:00")) is True  # 含边界
    assert q.event_visible(_dt("2024-01-01T15:00")) is True
    assert q.event_visible(_dt("2024-01-03T09:30")) is False  # 未来 bar 不可见


def test_published_visible_boundaries() -> None:
    q = PitQuery(as_of=_dt("2024-01-02T15:00"))
    assert q.published_visible(_dt("2024-01-02T15:00")) is True
    assert q.published_visible(_dt("2024-01-03T09:00")) is False  # 公布晚于知识时间


def test_row_visible_requires_both_times() -> None:
    # 报告期早于 as_of、但披露日晚于知识时间 → 不可见（防"财报未来函数"）
    q = PitQuery(as_of=_dt("2023-11-01T15:00"), knowledge_time=_dt("2023-11-01T15:00"))
    visible = q.row_visible(
        event_time=_dt("2023-09-30T00:00"),  # 报告期（事件时间）
        published_at=_dt("2023-10-28T00:00"),  # 披露日
    )
    assert visible is True

    not_yet = q.row_visible(
        event_time=_dt("2023-09-30T00:00"),
        published_at=_dt("2023-11-10T00:00"),  # 披露日晚于知识时间
    )
    assert not_yet is False


def test_explicit_knowledge_time_tightens_visibility() -> None:
    """知识时间可早于事件时间截止（如数据源次日才同步的场景）。"""
    as_of = _dt("2024-01-02T15:00")
    q = PitQuery(as_of=as_of, knowledge_time=_dt("2023-12-31T15:00"))
    # 事件在 as_of 内可见，但公布晚于知识时间 → 仍不可见
    assert q.row_visible(_dt("2024-01-01T15:00"), _dt("2024-01-02T09:00")) is False
