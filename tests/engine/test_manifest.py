# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : I3 RunManifest 单测：哈希工具/数据清单/聚合字段（设计 8.8）

"""RunManifest 组件测试（8.8 确定性清单）。"""

from __future__ import annotations

import json
from pathlib import Path

from mtzquant.engine.manifest import (
    build_data_manifest,
    build_manifest,
    canonical_json,
    config_hash,
    sha256_text,
)
from tests.fixtures.backtest_env import make_backtest_env


def test_sha_and_canonical() -> None:
    """哈希工具确定性（同输入同输出; 规范化 JSON 键排序）。"""
    assert sha256_text("abc") == sha256_text("abc")
    assert sha256_text("abc") != sha256_text("abd")
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert config_hash({"x": 1}) == config_hash({"x": 1})


def test_data_manifest_fingerprint(tmp_path: Path) -> None:
    """data_manifest: 逐 code 文件 sha256 + 行数 + 区间; 修订 → 指纹变化。"""
    env = make_backtest_env(tmp_path)
    pipe = _pipeline(env)
    m1 = build_data_manifest(pipe.driver, [env.code])
    entry = m1[env.code]
    assert entry["freq"] == "1d"
    assert entry["lines"] > 0
    # 篡改文件 → sha256 变化
    csv_path = env.data_root / "kline" / "etf" / "day" / f"{env.code}.csv"
    csv_path.write_text(csv_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    m2 = build_data_manifest(pipe.driver, [env.code])
    assert m1[env.code]["sha256"] != m2[env.code]["sha256"]


def test_build_manifest_fields(tmp_path: Path) -> None:
    """聚合清单字段齐全: strategy/data/config/seed/版本/git。"""
    env = make_backtest_env(tmp_path)
    pipe = _pipeline(env)
    task_dict = json.loads(env.task.model_dump_json())
    strategy_code = env.strategy_path.read_text(encoding="utf-8")
    manifest, manifest_hash = build_manifest(
        task_dict,
        strategy_code,
        driver=pipe.driver,
        universe=env.task.universe,
        settings=env.settings,
    )
    assert manifest["strategy_sha256"] == sha256_text(strategy_code)
    assert manifest["random_seed"] == 42
    assert manifest["mtzquant_version"]
    assert manifest["config_hash"] == config_hash(task_dict)
    assert "data_manifest" in manifest and env.code in manifest["data_manifest"]
    # manifest_hash == 规范化「指纹」（剔除 created_at 运行时刻）的 sha256（8.8 确定性聚合）
    fingerprint = {k: v for k, v in manifest.items() if k != "created_at"}
    assert manifest_hash == sha256_text(canonical_json(fingerprint))


def _pipeline(env):
    from mtzquant.engine.runner import build_pipeline

    return build_pipeline(env.settings, env.task.universe)
