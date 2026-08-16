# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:22:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-S02：WriteBuffer 批量 flush（条数/时间）+ 背压阻塞（设计 8.7）

"""T-S02：WriteBuffer（设计 8.7）——三触发与背压。"""

from __future__ import annotations

from mtzquant.store.write_buffer import BufferConfig, WriteBuffer


def test_batch_size_trigger() -> None:
    flushed: list = []
    buf = WriteBuffer(
        BufferConfig(batch_size=3, flush_interval_ms=1000, buffer_max_rows=1000),
        flush_callback=flushed.extend,
    )
    buf.add(1)
    buf.add(2)
    assert buf.buffered == 2  # 未达阈值
    buf.add(3)
    assert flushed == [1, 2, 3]  # 达 batch_size 自动 flush


def test_interval_trigger() -> None:
    now = [0.0]

    def fake_now() -> float:
        return now[0]

    flushed: list = []
    buf = WriteBuffer(
        BufferConfig(batch_size=100, flush_interval_ms=500, buffer_max_rows=1000),
        flush_callback=flushed.extend,
        now=fake_now,
    )
    buf.add(1)
    now[0] += 0.6  # 超 500ms → 时间触发
    buf.add(2)
    assert flushed == [1, 2]


def test_force_flush_at_end() -> None:
    flushed: list = []
    buf = WriteBuffer(
        BufferConfig(batch_size=100, flush_interval_ms=1000, buffer_max_rows=1000),
        flush_callback=flushed.extend,
    )
    buf.add("x")
    assert buf.flush() == 1  # 结束强制 flush
    assert flushed == ["x"]


def test_backpressure_blocking() -> None:
    """达内存上限 → 主循环阻塞 flush（宁慢不丢, 8.7）。"""
    flushed: list = []
    buf = WriteBuffer(
        BufferConfig(batch_size=1000, flush_interval_ms=60_000, buffer_max_rows=3),
        flush_callback=flushed.extend,
    )
    for i in range(10):
        buf.add(i)
    # 缓冲上限 3: add 第 4 条起触发背压 flush（每满 3 条刷一次）
    assert len(flushed) == 9
    assert buf.buffered == 1  # 末条留在缓冲（未触发, 结束强制 flush）
    buf.flush()
    assert len(flushed) == 10
