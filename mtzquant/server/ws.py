# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:10:00
# @update_time        : 2026/08/17 02:10:00
# @description : M4-W3 WebSocket 事件枢纽：多会话订阅 + resume 补帧 + bandwidth 节流（6.3/13.5）

"""WsHub（设计 6.3/5.6, M2-W0 最小版 → M4-W3 多会话）——回测事件流 → WS 订阅者。

- 信封: `{type, run_id, ts, event_seq, committed, data}`（6.3 字段冻结, 不改名）;
- 多会话: 每客户端可声明 `run_id` 过滤（不传 = 全部运行中会话）;
- **resume 补帧**: 连接时若 `last_event_seq`>0 → 从 run_event_journal（8.3.7）按 seq 回放
  缺失事件, 再续接实时流（journal_loader 由会话管理器注入, 断线重连无缺口, Y2）;
- bandwidth 节流: `bandwidth:low` 客户端 → 丢弃高频明细（order_event/fill/log 不推,
  daily_nav 每 5 日一推, progress 保留）——曲线等距采样以 DB 为准（6.1 性能预算）。

不拖慢主循环: 事件在 emit 时直发, 不经 WriteBuffer（8.7 推送与落库解耦）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from typing import Any

from mtzquant.engine.results import StoredRecord

# 信封字段与 6.3 完全一致（M4 冻结接口, W0 只扩不重写）
ENVELOPE_KEYS = ("type", "run_id", "ts", "event_seq", "committed", "data")

# 低带宽丢弃的高频明细类型（13.5: bandwidth:low 节流放大）
_LOW_BANDWIDTH_DROP = frozenset({"order_event", "fill", "log"})
_LOW_BANDWIDTH_NAV_EVERY = 5  # daily_nav 每 5 日一推（前端曲线以 DB 全量为准）


def envelope(rec: StoredRecord) -> dict[str, Any]:
    """StoredRecord → 6.3 信封（字段名逐一映射, 不做任何加工）。"""
    return {
        "type": rec.kind,
        "run_id": rec.run_id,
        "ts": rec.ts,
        "event_seq": rec.event_seq,
        "committed": rec.committed,
        "data": rec.payload,
    }


def _json(env: dict[str, Any]) -> str:
    return json.dumps(env, ensure_ascii=False, separators=(",", ":"))


class _Client:
    """单个订阅者（run_id 过滤 + 带宽档 + 节流状态）。"""

    __slots__ = ("queue", "loop", "run_id", "bandwidth", "_nav_count")

    def __init__(self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
        self.queue = queue
        self.loop = loop
        self.run_id: str | None = None
        self.bandwidth: str = "high"
        self._nav_count = 0

    def wants(self, env: dict[str, Any]) -> bool:
        """该客户端是否接收此事件（run_id 过滤 + 带宽节流）。"""
        if self.run_id is not None and env.get("run_id") != self.run_id:
            return False
        if self.bandwidth == "low":
            t = env.get("type")
            if t in _LOW_BANDWIDTH_DROP:
                return False
            if t == "daily_nav":
                self._nav_count += 1
                if self._nav_count % _LOW_BANDWIDTH_NAV_EVERY != 1:
                    return False
        return True


class WsHub:
    """事件枢纽: 回放 + 实时广播（线程安全; 多会话, M4-W3）。"""

    def __init__(
        self,
        journal_loader: Callable[[str, int], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._journal_loader = journal_loader  # (run_id, after_seq) -> 信封列表（resume 源）
        self._store: Any = None  # W0 进程内回放源（attach; 保持旧版兼容）
        self._clients: dict[int, _Client] = {}
        self._lock = threading.Lock()
        self._next_id = 0

    def attach(self, store: Any) -> None:
        """挂接进程内回放源（W0 兼容: run_local 先建枢纽后建 ResultStore 时使用, 6.3 回放）。"""
        self._store = store

    # ------------------------------------------------------------------
    # 连接生命周期（WS 端点调用; 须在事件循环线程内执行）
    # ------------------------------------------------------------------
    def connect(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        run_id: str | None = None,
        last_event_seq: int = 0,
        bandwidth: str = "high",
    ) -> asyncio.Queue[str]:
        """注册新客户端: 先补帧（journal 断点续传 / W0 进程内 store 回放）, 再续接实时流。"""
        loop = loop or asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()
        client = _Client(queue, loop)
        client.run_id = run_id
        client.bandwidth = "low" if bandwidth == "low" else "high"
        with self._lock:
            # 锁内补帧: 与 publish 互斥, 保证「补帧 ∪ 实时」恰好覆盖全量事件（6.3）
            replay: list[dict[str, Any]] = []
            if last_event_seq > 0 and run_id is not None and self._journal_loader is not None:
                replay = self._journal_loader(run_id, last_event_seq)
            elif self._store is not None:
                replay = [envelope(r) for r in self._store.all_records()]
            for env in replay:
                if client.wants(env):
                    queue.put_nowait(_json(env))
            cid = self._next_id
            self._next_id += 1
            self._clients[cid] = client
        return queue

    def disconnect(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            for cid, client in list(self._clients.items()):
                if client.queue is queue:
                    del self._clients[cid]
                    return

    # ------------------------------------------------------------------
    # 事件发布（回测线程/会话管理器调用）
    # ------------------------------------------------------------------
    def publish(self, rec: StoredRecord) -> None:
        """StoredRecord → 信封 → 广播（W0 兼容: 进程内 ResultStore publish_hook）。"""
        self.publish_envelope(envelope(rec))

    def publish_envelope(self, env: dict[str, Any]) -> None:
        """把一条事件信封推给匹配客户端（跨线程投递, 不阻塞主循环; W5 发布桥）。"""
        msg = _json(env)
        with self._lock:
            targets = [c for c in self._clients.values() if c.wants(env)]
        for client in targets:
            try:
                client.loop.call_soon_threadsafe(client.queue.put_nowait, msg)
            except RuntimeError:
                pass  # 事件循环已关闭（服务停机竞态, 丢弃即可）

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)
