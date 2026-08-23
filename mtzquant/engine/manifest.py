# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/23 12:10:00
# @description : I3 RunManifest：确定性重放清单（strategy/data/config 哈希聚合, 设计 8.8）

"""RunManifest（设计 8.8）——确定性重放清单与治理。

采集项:
  strategy_sha256     策略源码 sha256（8.3.2 与 strategy_snapshot 同源）
  mtzquant_version + git_commit + dirty（阶段 A D3: 依赖 git commit）
  python_version / deps_lock_hash
  data_manifest       逐 (code, freq) 源 CSV: sha256 + 行数 + 区间; 无源文件(缓存命中)按
                      mtime+size 落 sig 基线（3.7 缓存失效判据, 8.8 数据修订可定位）
  calendar/master/corp_actions 版本（文件哈希, 缺失记 "none"）
  config_hash         任务配置规范化（排序键 JSON）后哈希
  random_seed=42      确定性纪律（引擎内禁未播种随机）
  manifest_hash       全部字段规范化 JSON 聚合 sha256

确定性治理（8.8）: 所有 JSON 序列化 sort_keys=True; dict 遍历排序; 时间整数毫秒。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mtzquant import __version__
from mtzquant.config import Settings
from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import Frequency
from mtzquant.data.drivers.base import SourceDriver

RANDOM_SEED = 42  # 确定性纪律（8.8）: 引擎内禁未播种随机


# ------------------------------------------------------------------
# 哈希工具
# ------------------------------------------------------------------
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> str:
    """规范化 JSON（确定性 8.8: 排序键 + 紧凑分隔, 供哈希/比对）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_hash(task: dict[str, Any]) -> str:
    """任务配置规范化哈希（随机数/时间戳等非确定性字段剔除由调用方先清理）。"""
    return sha256_text(canonical_json(task))


# ------------------------------------------------------------------
# git / 环境
# ------------------------------------------------------------------
def git_revision(root: Path) -> tuple[str | None, bool]:
    """返回 (git_commit, dirty); 非 git 仓库/无 git → (None, False)。"""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if head.returncode != 0:
            return None, False
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return head.stdout.strip() or None, bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, False


def python_version() -> str:
    return platform.python_version()


def deps_lock_hash() -> str:
    """依赖清单哈希（pip freeze 无法保证排序 → 规范化后哈希; 失败记空）。"""
    try:
        freeze = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if freeze.returncode == 0:
            lines = sorted(line for line in freeze.stdout.splitlines() if line.strip())
            return sha256_text("\n".join(lines))
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ------------------------------------------------------------------
# 数据清单
# ------------------------------------------------------------------
@dataclass
class DataFileEntry:
    """单个数据文件的可复现指纹（8.8: 数据修订 → sha256 变化）。"""

    code: str
    freq: str
    path: str
    sha256: str
    lines: int
    start: str | None = None  # 首根交易日（YYYY-MM-DD）
    end: str | None = None  # 末根交易日


def build_data_manifest(
    driver: SourceDriver, codes: list[str], frequency: Frequency = Frequency.D1
) -> dict[str, dict[str, Any]]:
    """逐 (code, freq) 源文件指纹; 文件缺失 → 记 error（数据层会先报错, 8.8）。"""
    out: dict[str, dict[str, Any]] = {}
    for code in sorted(set(codes)):
        try:
            path = driver.kline_path(code, frequency)  # type: ignore[attr-defined]
        except MtzQuantError as exc:
            out[code] = {"error": str(exc.message)}
            continue
        if not Path(path).is_file():
            out[code] = {"error": "missing file", "path": str(path)}
            continue
        text = Path(path).read_text(encoding=driver.settings.encoding)  # type: ignore[attr-defined]
        lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        entry = DataFileEntry(
            code=code,
            freq=frequency.value,
            path=str(path),
            sha256=sha256_text(text),
            lines=lines,
        )
        out[code] = asdict(entry)
    return out


# ------------------------------------------------------------------
# 聚合清单
# ------------------------------------------------------------------
def build_manifest(
    task: dict[str, Any],
    strategy_code: str,
    *,
    driver: SourceDriver,
    universe: list[str],
    settings: Settings | None = None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """构建 RunManifest; 返回 (manifest, manifest_hash)（8.8 聚合）。"""
    root = project_root or Path(__file__).resolve().parents[2]
    commit, dirty = git_revision(root)

    # 数据清单（universe 标的 + 基准）
    data_codes = list(universe)
    data_manifest = build_data_manifest(driver, data_codes)

    # 日历/master/公司行为版本（缺失 → "none"）
    versions = _asset_versions(driver, root)

    manifest: dict[str, Any] = {
        "strategy_sha256": sha256_text(strategy_code),
        "mtzquant_version": __version__,
        "git_commit": commit,
        "git_dirty": dirty,
        "python_version": python_version(),
        "deps_lock_hash": deps_lock_hash(),
        "data_manifest": data_manifest,
        "calendar_version": versions["calendar"],
        "master_version": versions["master"],
        "corp_actions_version": versions["corp_actions"],
        "config_hash": config_hash(task),
        "random_seed": RANDOM_SEED,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    # M3-T3/U1: 扫描上下文（selection_count=该参数共尝试组数）与 OOS 段标记（parent_lineage.split）
    engine = task.get("engine") or {}
    if "selection_count" in engine:
        manifest["selection_count"] = engine["selection_count"]
    pl = engine.get("parent_lineage") or {}
    if pl:
        manifest["parent_lineage"] = pl
    # 指纹 = 剔除运行时刻等非确定性字段（8.8: 同输入 manifest_hash 全等, 供 replay 比对）
    fingerprint = {k: v for k, v in manifest.items() if k != "created_at"}
    manifest_hash = sha256_text(canonical_json(fingerprint))
    return manifest, manifest_hash


def _asset_versions(driver: SourceDriver, root: Path) -> dict[str, str]:
    """日历/master/公司行为目录版本（文件存在则哈希, 缺失记 'none'）。"""
    lcs = driver.settings  # type: ignore[attr-defined]
    data_root = Path(lcs.root_path)
    candidates: dict[str, list[Path]] = {
        "calendar": [
            data_root / lcs.calendar_dir / "trade_days.csv",
        ],
        "master": [
            data_root / lcs.master_dir / "instruments.csv",
        ],
    }
    # 公司行为: corporate_actions/{type} 占位剥离后 rglob 全量 .csv（设计 3.12）
    corp_dir = data_root / Path(str(lcs.corporate_actions_dir).split("{type}")[0])
    corp_files = sorted(corp_dir.rglob("*.csv")) if corp_dir.is_dir() else []
    candidates["corp_actions"] = corp_files
    out: dict[str, str] = {}
    for key, paths in candidates.items():
        files = [p for p in paths if p.is_file()]
        if not files:
            out[key] = "none"
            continue
        out[key] = sha256_text("\n".join(f"{p}:{sha256_file(p)}" for p in sorted(files)))
    return out
