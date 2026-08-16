# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:14:00
# @update_time        : 2026/08/16 03:14:00
# @description : G4 WriteBuffer：批量写缓冲（条数/时间/内存三触发 + 背压, 设计 8.7）

"""WriteBuffer（设计 8.7）——回测明细批量入库缓冲。

三条件触发 flush: 条数(默认 500) / 时间(500ms) / 内存上限(5 万行背压阻塞)。
关键时点（暂停/终止/异常/结束）由调用方强制 flush; 背压: 达 buffer_max_rows
主循环同步阻塞等待（宁慢不丢, 8.7 纪律）。

并发纪律: 主循环单线程写; flush 委托 repo 执行 executemany 批量插入。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class BufferConfig:
    """写缓冲参数（对应 settings.database 配置）。"""

    batch_size: int = 500
    flush_interval_ms: int = 500
    buffer_max_rows: int = 50_000


class WriteBuffer:
    """批量写缓冲（线程安全: 主循环写 / flush 钩子异步落库）。"""

    def __init__(
        self,
        config: BufferConfig | None = None,
        *,
        flush_callback: Callable[[list[Any]], None] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or BufferConfig()
        self._flush_callback = flush_callback
        self._now = now or time.monotonic
        self._rows: list[Any] = []
        self._lock = threading.Lock()
        self._last_flush = self._now()
        self.flush_count = 0

    # ------------------------------------------------------------------
    def add(self, row: Any) -> None:
        """入缓冲; 达阈值触发 flush; 达内存上限阻塞背压（8.7 宁慢不丢）。"""
        with self._lock:
            self._rows.append(row)
            if len(self._rows) >= self.config.batch_size:
                self._flush_locked()
                return
            elapsed_ms = (self._now() - self._last_flush) * 1000
            if elapsed_ms >= self.config.flush_interval_ms:
                self._flush_locked()
                return
            # 背压: 达内存上限, 阻塞 flush 直至低于阈值（宁慢不丢）
            while len(self._rows) >= self.config.buffer_max_rows:
                self._flush_locked()

    def _flush_locked(self) -> int:
        """（持锁）刷出全部缓冲行; 返回刷出行数。"""
        if not self._rows:
            return 0
        batch, self._rows = self._rows, []
        self._last_flush = self._now()
        if self._flush_callback is not None:
            self._flush_callback(batch)
        self.flush_count += 1
        return len(batch)

    def flush(self) -> int:
        """强制刷出全部缓冲行（关键时点/结束调用）。"""
        with self._lock:
            return self._flush_locked()

    @property
    def buffered(self) -> int:
        return len(self._rows)

    def __len__(self) -> int:
        return len(self._rows)
