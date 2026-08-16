# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 14:50:00
# @update_time        : 2026/08/16 21:59:08
# @description : O4 防封禁共用件：每源令牌桶+抖动+自动降档+熔断（3.9 防封禁表）

"""远程源限流与防封禁共用件（设计 3.9 防封禁表）——从 fetch_etf 提炼, 多源共用。

- `TokenBucketLimiter`: 令牌桶（速率 + 0.5~1.5× 随机抖动, 8.8 确定性 seed=42）;
- `CircuitBreaker`: 同源连续失败达阈值 → 暂停一段时间（熔断, 封禁类响应不再重试）;
- `RateLimitController`: 每源状态机——令牌桶 + 抖动 + **自动降档**（连续命中限流速率
  减半, 成功后缓慢恢复）+ 熔断; 是远程源驱动统一走的等待/记录入口。

语义（3.9）: akshare 1 次/秒, tushare 120 次/分; 指数退避重试在驱动侧用
`RetryPolicy`（2^n 底数, 含抖动）, 封禁类响应（积分不足/黑名单）直接抛不重试。
"""

from __future__ import annotations

import random
import time as time_mod
from collections.abc import Callable
from dataclasses import dataclass

from mtzquant.core.errors import MtzQuantError


# 每源基础速率（3.9 防封禁表; baostock 并发=1 由并发信号量承载, M5 预留）
@dataclass(frozen=True)
class RateSpec:
    rate_per_min: int
    jitter_range: tuple[float, float] = (0.5, 1.5)


SOURCE_RATES: dict[str, RateSpec] = {
    "tushare": RateSpec(rate_per_min=120),
    "akshare": RateSpec(rate_per_min=60),  # 1 次/秒
    "baostock": RateSpec(rate_per_min=60),  # M5 预留（并发=1）
}


class TokenBucketLimiter:
    """令牌桶限流（3.9: 速率 + 抖动; 注入 now 便于测试）。"""

    def __init__(
        self,
        rate_per_min: int = 60,
        *,
        jitter_range: tuple[float, float] = (0.5, 1.5),
        rng: random.Random | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.rate_per_min = max(1, rate_per_min)
        self.interval = 60.0 / self.rate_per_min
        self.jitter_range = jitter_range
        self.rng = rng or random.Random(42)  # 确定性纪律 8.8
        self._now = now or time_mod.monotonic
        self._last = -float("inf")

    def wait(self) -> float:
        """按需等待以保持速率; 返回本次实际等待秒数（含抖动）。

        语义: 相邻两次许可的启动时刻间隔 ≈ interval（含 0.5~1.5× 抖动）——
        首次立即放行, 之后若过早请求则等待补足。
        """
        interval = self.interval * self.rng.uniform(*self.jitter_range)
        now = self._now()
        wait = max(0.0, self._last - now)
        self._last = max(self._last, now) + interval
        return wait


class CircuitBreaker:
    """同源熔断（3.9: 连续失败达阈值 → 暂停; 供 RateLimitController 内部用）。"""

    def __init__(
        self,
        *,
        threshold: int = 5,
        pause_seconds: float = 600.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._threshold = threshold
        self._pause = pause_seconds
        self._now = now or time_mod.monotonic
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def record_success(self, source: str) -> None:
        self._fails[source] = 0
        self._open_until.pop(source, None)  # 成功 = 源已恢复, 关闭熔断（3.9）

    def record_failure(self, source: str) -> None:
        self._fails[source] = self._fails.get(source, 0) + 1
        if self._fails[source] >= self._threshold:
            self._open_until[source] = self._now() + self._pause
            self._fails[source] = 0  # 熔断打开后计数清零, 重新计时

    def is_open(self, source: str) -> bool:
        until = self._open_until.get(source, 0.0)
        if until <= self._now():
            return False
        return True

    def remaining(self, source: str) -> float:
        """熔断剩余等待秒数（未打开 → 0）。"""
        return max(0.0, self._open_until.get(source, 0.0) - self._now())


class RetryPolicy:
    """指数退避重试（2^n × 抖动, 3.9; max_retry 次后放弃）。"""

    def __init__(
        self,
        max_retries: int = 3,
        *,
        base: float = 2.0,
        jitter_range: tuple[float, float] = (0.5, 1.5),
        rng: random.Random | None = None,
    ) -> None:
        self.max_retries = max_retries
        self._base = base
        self._jitter = jitter_range
        self._rng = rng or random.Random(42)

    def next_delay(self, attempt: int) -> float:
        """第 attempt 次失败后（attempt 从 0 起）的退避秒数 = 2^attempt × 抖动。"""
        return (self._base**attempt) * self._rng.uniform(*self._jitter)

    def allow_retry(self, attempt: int) -> bool:
        return attempt < self.max_retries


class _SourceState:
    """单源运行态（当前速率 = 基础速率经降档/恢复后的值）。"""

    def __init__(self, spec: RateSpec, rng: random.Random, now: Callable[[], float]) -> None:
        self.spec = spec
        self.base_rate = max(1.0, float(spec.rate_per_min))
        self.current_rate = self.base_rate
        self._rng = rng
        self._now = now
        self._last = -float("inf")

    def wait_interval(self) -> float:
        interval = (60.0 / self.current_rate) * self._rng.uniform(*self.spec.jitter_range)
        now = self._now()
        wait = max(0.0, self._last - now)
        self._last = max(self._last, now) + interval
        return wait

    def downshift(self, ratio: float = 0.5) -> None:
        """自动降档: 连续命中限流 → 速率减半（下限 = 基础速率 × min_ratio, 防降到 0）。"""
        floor = self.base_rate * 0.25
        self.current_rate = max(floor, self.current_rate * ratio)

    def recover(self, step: float = 0.1) -> None:
        """成功后缓慢恢复: 当前速率朝基础速率靠拢。"""
        if self.current_rate < self.base_rate:
            self.current_rate = min(self.base_rate, self.current_rate * (1.0 + step))


class RateLimitController:
    """每源限流控制器（令牌桶 + 抖动 + 自动降档 + 熔断, 3.9）。

    `wait(source)` 在任何远程请求前调用（含熔断等待）; 请求成功/命中限流/失败后
    分别调 `record_success / record_rate_limited / record_failure`。
    """

    def __init__(
        self,
        specs: dict[str, RateSpec] | None = None,
        *,
        breaker_threshold: int = 5,
        breaker_pause_seconds: float = 600.0,
        rng: random.Random | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._specs = specs if specs is not None else dict(SOURCE_RATES)
        self._rng = rng or random.Random(42)
        self._now = now or time_mod.monotonic
        self._breaker = CircuitBreaker(
            threshold=breaker_threshold, pause_seconds=breaker_pause_seconds, now=self._now
        )
        self._state: dict[str, _SourceState] = {}

    def _state_of(self, source: str) -> _SourceState:
        st = self._state.get(source)
        if st is None:
            spec = self._specs.get(source)
            if spec is None:
                raise MtzQuantError(
                    f"未知远程源 {source!r}", stage="ratelimit", hint=f"可选: {sorted(self._specs)}"
                )
            st = _SourceState(spec, self._rng, self._now)
            self._state[source] = st
        return st

    def wait(self, source: str) -> float:
        """请求前等待（含令牌桶间隔 + 熔断剩余）。返回等待秒数。"""
        st = self._state_of(source)
        if self._breaker.is_open(source):
            return self._breaker.remaining(source)
        return st.wait_interval()

    def record_success(self, source: str) -> None:
        self._breaker.record_success(source)
        self._state_of(source).recover()

    def record_rate_limited(self, source: str) -> None:
        """命中限流 → 降档（3.9 自动降档）; 不算熔断失败（服务可用, 只是慢）。"""
        self._state_of(source).downshift()

    def record_failure(self, source: str) -> None:
        self._breaker.record_failure(source)

    def is_breached(self, source: str) -> bool:
        return self._breaker.is_open(source)

    def current_rate(self, source: str) -> float:
        return self._state_of(source).current_rate
