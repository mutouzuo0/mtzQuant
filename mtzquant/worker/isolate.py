# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : I5 worker 隔离：subprocess 跑引擎（环境清洁 + 超时进程树 terminate, 设计 2.4）

"""worker 隔离（设计 2.4）——`mtzquant run --isolate` 的最小实现。

环境清洁: 剔除 *_TOKEN / *_API_KEY / *_SECRET / *_PASSWORD / *WEBHOOK 环境变量
        （密钥纪律 3.6: 子进程拿不到宿主密钥）。
超时:     默认 1h（可配）; 超时对 Windows 用 taskkill /T（进程树）, 其余 SIGKILL。
同进程为默认（本地可信）; --isolate 仅在不可信策略/数据源场景启用。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mtzquant.core.errors import MtzQuantError

# 环境清洁模式（密钥纪律 3.6: 子进程剔除一切敏感环境变量）
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN",
    "API_KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "WEBHOOK",
    "CREDENTIAL",
)

DEFAULT_TIMEOUT_SECONDS = 3600  # 默认 1h（2.4）


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """剔除敏感环境变量, 返回净化后的副本（T-X03 断言）。"""
    src = dict(env if env is not None else os.environ)
    return {
        k: v for k, v in src.items() if not any(sub in k.upper() for sub in _SENSITIVE_SUBSTRINGS)
    }


@dataclass(frozen=True)
class WorkerResult:
    """隔离运行结果（T-X03 断言: 超时/非零退出）。"""

    returncode: int
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""


def run_isolated(
    cmd: list[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> WorkerResult:
    """以净化环境运行子进程; 超时终止进程树（2.4）。"""
    clean = clean_env(env)
    try:
        proc = subprocess.Popen(
            cmd,
            env=clean,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise MtzQuantError(
            f"无法启动隔离子进程: {cmd[0]}",
            stage="worker",
            hint=f"原因为 {exc}; 检查解释器路径与 `python -m mtzquant` 可用性",
        ) from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return WorkerResult(returncode=proc.returncode or 0, stdout=stdout, stderr=stderr)
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)
        out, err = proc.communicate()
        return WorkerResult(
            returncode=124,  # 与 timeout(1) 语义一致
            timed_out=True,
            stdout=out or "",
            stderr=(err or "") + f"\n[worker] 超时 {timeout_seconds}s, 进程树已终止",
        )


def _terminate_tree(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """终止进程树（Windows: taskkill /T /F; POSIX: SIGKILL 组）。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()


def isolate_python_command(extra: list[str], *, python: Path | None = None) -> list[str]:
    """构造 `python -m mtzquant <args>` 命令（worker 子进程入口）。"""
    return [str(python or sys.executable), "-m", "mtzquant", *extra]
