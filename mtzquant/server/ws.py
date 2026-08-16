# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:10:00
# @update_time        : 2026/08/16 21:59:08
# @description : W0-3 WebSocket 事件枢纽：ResultStore 信封直发（6.3）+ 重连回放 + 多客户端 fan-out

"""WsHub（设计 6.3/5.6, M2-W0 最小版）——回测事件流 → WS 订阅者。

- 信封: `{type, run_id, ts, event_seq, committed, data}`（与 6.3 字段完全一致;
  ResultStore 已产出 `StoredRecord(kind/run_id/ts/event_seq/committed/payload)`, 只做字段名投影）。
- 线程模型: 回测在独立线程跑, WS 事件循环在 uvicorn 线程; publish 经
  `loop.call_soon_threadsafe` 从回测线程投递到各客户端 asyncio.Queue, 线程安全。
- 回放: 连接建立时先把 `store.all_records()`（已 flush 者 committed:true 快照）整批入队,
  再续接实时流——resume 的简化实现（固定 seq=1 起; 断点续传 `last_event_seq` 归 M4-W3）。
- 多客户端: 每客户端独立队列, 各自按 seq 单调收全量事件（一致性由 emit 单调 + 锁内互斥保证）。

不拖慢主循环: 事件在 emit 时直发, 不经 WriteBuffer（8.7 推送与落库解耦）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from mtzquant.engine.results import StoredRecord

# 信封字段与 6.3 完全一致（M4 冻结接口, W0 只扩不重写）
ENVELOPE_KEYS = ("type", "run_id", "ts", "event_seq", "committed", "data")


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


class WsHub:
    """事件枢纽: 回放 + 实时广播（线程安全）。"""

    def __init__(self, store: Any | None = None) -> None:
        self._store = store  # ResultStore（回放源; 允许 None 便于纯单元测试）
        self._clients: dict[int, tuple[asyncio.Queue[str], asyncio.AbstractEventLoop]] = {}
        self._lock = threading.Lock()
        self._next_id = 0

    def attach(self, store: Any) -> None:
        """挂接回放源（run_local 先建枢纽后建 ResultStore 时使用, 6.3 回放）。"""
        self._store = store

    # ------------------------------------------------------------------
    # 连接生命周期（WS 端点调用; 须在事件循环线程内执行）
    # ------------------------------------------------------------------
    def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Queue[str]:
        """注册新客户端: 先回放已发布记录, 再续接实时流（锁内完成防丢帧/重帧）。"""
        loop = loop or asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()
        with self._lock:
            # 锁内回放: 与 publish 互斥, 保证「回放∪实时」恰好覆盖全量事件
            if self._store is not None:
                for rec in self._store.all_records():
                    queue.put_nowait(json.dumps(envelope(rec), ensure_ascii=False))
            cid = self._next_id
            self._next_id += 1
            self._clients[cid] = (queue, loop)
        return queue

    def disconnect(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            for cid, (q, _loop) in list(self._clients.items()):
                if q is queue:
                    del self._clients[cid]
                    return

    # ------------------------------------------------------------------
    # 事件发布（回测线程调用）
    # ------------------------------------------------------------------
    def publish(self, rec: StoredRecord) -> None:
        """把一条事件信封推给所有在线客户端（跨线程投递, 不阻塞主循环）。"""
        msg = json.dumps(envelope(rec), ensure_ascii=False)
        with self._lock:
            targets = list(self._clients.values())
        for queue, loop in targets:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, msg)
            except RuntimeError:
                pass  # 事件循环已关闭（服务停机竞态, 丢弃即可）

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)
