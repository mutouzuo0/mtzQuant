# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 21:33:45
# @description : 允许 `python -m mtzquant` 调用 CLI

"""允许 `python -m mtzquant` 调用 CLI。"""

from __future__ import annotations

from mtzquant.cli import app

if __name__ == "__main__":
    app()
