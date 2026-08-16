# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:55:00
# @description : g09 动态 universe 切换：history 可见性 + 懒加载计数 + 缓存命中

"""黄金用例 g09：动态 universe 切换（测试方案 §5 g09）。

场景：初始池 [A,B]；day30 策略 set_universe([A,B,C])。
断言：
- day31 起 C 可查询（history 完整覆盖前 30 日，含 warmup 预热）；
- day31 前查询 C → 报错（未进 universe）；
- A/B 懒加载：首次访问触发加载，二次访问命中缓存不重复解析（load_count 不变）。
"""

from __future__ import annotations

import pytest

from mtzquant.core.errors import MtzQuantError

from .conftest import flat_series
from .daily import DailyDriver
from .framework import MockBroker

A = "600000.SH"
B = "510300.SH"
C = "159915.SZ"
N = 40


@pytest.fixture()
def tri_driver() -> DailyDriver:
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    driver.add_data(
        {
            A: flat_series(A, N),
            B: flat_series(B, N),
            C: flat_series(C, N),
        }
    )
    driver.set_universe([A, B])
    return driver


def test_g09_universe_gate_before_add(tri_driver) -> None:
    """C 未进 universe 前查询 → 报错；A/B 正常。"""
    driver = tri_driver
    with pytest.raises(MtzQuantError, match="不在动态 universe"):
        driver.history(C, 5)
    assert len(driver.history(A, 5)) == 5


def test_g09_add_code_at_day30(tri_driver) -> None:
    """day30 set_universe([A,B,C]) → day31 起 C 可查询且完整覆盖。"""
    driver = tri_driver
    d30 = driver.sessions[29].bar.date
    d31 = driver.sessions[30].bar.date
    seen: list = []

    def _switch() -> None:
        driver.set_universe([A, B, C])

    def _query_c() -> None:
        bars = driver.history(C, N)
        seen.append(len(bars))
        assert len(bars) == 31  # d31 可见（含 d30 起 31 根）

    driver.on(d30, _switch)
    driver.on(d31, _query_c)
    driver.run()
    assert seen == [31]


def test_g09_lazy_load_count_cached(tri_driver) -> None:
    """懒加载：首次访问 +1，二次访问命中缓存不重复解析。"""
    driver = tri_driver
    d0 = driver.sessions[0].bar.date

    def _query_twice() -> None:
        driver.history(A, 5)
        driver.history(A, 5)  # 命中缓存
        assert driver.load_count(A) == 1

    driver.on(d0, _query_twice)
    driver.run()
    assert driver.load_count(A) == 1
    assert driver.load_count(B) == 0  # 未访问不触发加载
