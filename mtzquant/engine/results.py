# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:40:00
# @update_time        : 2026/08/16 09:05:00
# @description : F7 ResultStore：事件日志唯一来源（event_seq 信封）+ 写缓冲（8.7）

"""ResultStore（设计 5.6/8.7）——回测事件日志的唯一持久化来源 + 写缓冲。

  发布: 每条记录（Fill/OrderEvent/DailyNav/CorpAction/Log）→ {event_seq 单调递增,
        committed:false} 进写缓冲; M4 实时可视直接消费 event_seq 信封（6.3 不加传输层）。
  finalize: 全部记录定稿（指标等最后覆写时点, G/H 阶段接入）。
  缓冲: 三条件触发 flush（条数/时间/内存上限, 8.7 参数化）; 关键时点强制 flush;
        背压语义由调用方（引擎）实现同步阻塞（宁慢不丢）。

并发纪律: 主循环单线程写; 只追加（append-only, 8.3.7）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def _now_ms() -> int:
    """整数毫秒时间戳（8.8 时间戳整数毫秒; WS 展示用, 不入库不计确定性地）。"""
    return int(time.time() * 1000)


@dataclass
class StoredRecord:
    """事件日志单条（event_seq 信封, 6.3; M2-W0 补 run_id/ts 后直发 WS）。"""

    event_seq: int  # 单调递增（确定性, 8.8）
    kind: str  # fill / order_event / daily_nav / corp_action / log / metric / progress / status
    payload: dict[str, Any]  # 业务字段
    committed: bool = False  # 是否已 flush 落库（M4/WS 可识别进度）
    run_id: str = ""  # 归属 run（6.3 信封字段; 缺省空串兼容独立构造/单元测试）
    ts: int = 0  # 发布时刻（整数毫秒, 8.8）; 0=未设（旧调用/测试直接构造）


@dataclass
class FlushPolicy:
    """写缓冲触发条件（8.7 参数化）。"""

    batch_size: int = 500  # 条数
    flush_interval_ms: int = 500  # 时间
    buffer_max_rows: int = 50_000  # 内存上限（达此值主循环应背压等待）


class ResultStore:
    """事件日志唯一来源（append-only）。"""

    def __init__(
        self,
        policy: FlushPolicy | None = None,
        *,
        run_id: str | None = None,
        flush_hook: Callable[[list[StoredRecord]], None] | None = None,
        publish_hook: Callable[[StoredRecord], None] | None = None,
    ) -> None:
        self.policy = policy or FlushPolicy()
        self.run_id = run_id or ""  # 信封 run_id（会话装配时若为空会补, session.py）
        self._flush_hook = flush_hook  # 落库钩子（G 阶段 WriteBuffer/repo 接入）
        self._publish_hook = publish_hook  # 实时转发钩子（M2-W0 WS fan-out, 8.7 与落库解耦）
        self._records: list[StoredRecord] = []
        self._seq = 0
        self._flushed: list[StoredRecord] = []

    # ------------------------------------------------------------------
    def emit(self, kind: str, payload: dict[str, Any]) -> int:
        """发布一条事件; 返回 event_seq（M4 推送给前端）。

        先入缓冲再触发 publish_hook（实时消费者能立即拿到 committed:false 事件;
        已落库事件经 flush 置 committed:true, 重连回放可见快照语义, 6.3）。
        """
        self._seq += 1
        rec = StoredRecord(
            event_seq=self._seq,
            kind=kind,
            payload=dict(payload),
            run_id=self.run_id,
            ts=_now_ms(),
        )
        self._records.append(rec)
        if self._publish_hook is not None:
            self._publish_hook(rec)
        self._maybe_flush()
        return self._seq

    def _maybe_flush(self) -> None:
        if len(self._records) >= self.policy.batch_size:
            self.flush()

    def flush(self) -> int:
        """强制刷出已发布记录（关键时点/批量满/结束时调用, 8.7）。"""
        if not self._records:
            return 0
        batch = self._records
        self._records = []
        for rec in batch:
            rec.committed = True
        if self._flush_hook is not None:
            self._flush_hook(batch)
        self._flushed.extend(batch)
        return len(batch)

    # ------------------------------------------------------------------
    def finalize(self, *, force_flush: bool = True) -> list[StoredRecord]:
        """结束回测: 强制 flush, 返回全部已定稿记录（H 阶段在此覆写 metrics）。"""
        if force_flush:
            self.flush()
        return list(self._flushed)

    @property
    def buffered_count(self) -> int:
        return len(self._records)

    @property
    def total_count(self) -> int:
        return self._seq

    def all_records(self) -> list[StoredRecord]:
        return list(self._flushed) + list(self._records)

    def next_seq(self) -> int:
        return self._seq + 1
