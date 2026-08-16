# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 14:40:00
# @update_time        : 2026/08/16 21:59:08
# @description : O2 DuckDB 查询封装：read_csv_auto 直读 + 只读安全化 + 质量体检 SQL 模板（3.10）

"""DuckDB 查询封装（设计 3.10）——覆盖检查与数据体检的 SQL 落点。

- `read_csv_auto` 直读单文件与 glob 目录（`filename=true` 跨品种扫描带 _file 列）;
- **只读安全化**：`execute_select` 仅允许 SELECT/WITH/SHOW/DESCRIBE/EXPLAIN，
  拒绝任何 DDL/DML/PRAGMA/SET 等写面关键字——`mtzquant sql` 即席查询不走写路径;
- 数据质量体检 SQL 模板（缺失交易日/0价0量/OHLC 越界/重复日期/日期解析失败）。

纪律：本模块不做业务归一（那是 DataNormalizer 的职责），只做「DuckDB 直读 + 只读查询」。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 只读安全化：语句必须以这些前缀开头（3.10）
_SELECT_PREFIXES = ("select", "with", "show", "describe", "explain")

# 写面/副作用关键字（token 级匹配, 拒绝即抛; 3.10 只读）
_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "attach",
    "detach",
    "copy",
    "export",
    "import",
    "pragma",
    "set",
    "call",
    "vacuum",
    "install",
    "load",
    "grant",
    "revoke",
    "truncate",
    "refresh",
    "checkpoint",
    "begin",
    "commit",
    "rollback",
    "prepare",
    "execute",
    "macro",
)

# 质量体检 SQL 模板（{path} = read_csv_auto 源, 3.10）
# - missing_weekday: 覆盖区间内周一~周五缺失的交易日（轻量近似, 不含节假日日历）
# - zero_price_volume: 0 价 0 量行（疑似脏数据）
# - ohlc_out_of_bounds: high<max(open,close) / low>min(open,close) / high<low
# - duplicate_dates: trade_date 重复行
# - parse_fail: trade_date 无法按 %Y%m%d 解析
QUALITY_CHECKS: dict[str, str] = {
    "missing_weekday": """
        WITH src AS (
            SELECT STRPTIME(trade_date, '%Y%m%d')::DATE AS d
            FROM read_csv_auto('{path}', header=true, sample_size=100000)
        ),
        span AS (SELECT MIN(d) AS lo, MAX(d) AS hi FROM src),
        days AS (
            SELECT range::DATE AS d
            FROM range((SELECT lo FROM span), (SELECT hi FROM span) + INTERVAL '1 day',
                       INTERVAL '1 day')
            WHERE dayofweek(range::DATE) BETWEEN 1 AND 5
        )
        SELECT d AS missing_date FROM days
        WHERE d NOT IN (SELECT d FROM src)
        ORDER BY d
        """,
    "zero_price_volume": """
        SELECT trade_date, open, high, low, close, vol
        FROM read_csv_auto('{path}', header=true, sample_size=100000)
        WHERE (close = 0 OR (open = 0 AND vol = 0))
        ORDER BY trade_date
        """,
    "ohlc_out_of_bounds": """
        SELECT trade_date, open, high, low, close
        FROM read_csv_auto('{path}', header=true, sample_size=100000)
        WHERE high < low
           OR high < GREATEST(open, close)
           OR low > LEAST(open, close)
        ORDER BY trade_date
        """,
    "duplicate_dates": """
        SELECT trade_date, COUNT(*) AS n
        FROM read_csv_auto('{path}', header=true, sample_size=100000)
        GROUP BY trade_date
        HAVING COUNT(*) > 1
        ORDER BY trade_date
        """,
    "parse_fail": """
        SELECT trade_date
        FROM read_csv_auto('{path}', header=true, sample_size=100000)
        WHERE TRY_STRPTIME(trade_date, '%Y%m%d') IS NULL
        ORDER BY trade_date
        """,
}


def _strip_sql_comment(sql: str) -> str:
    """剥掉 SQL 注释（-- 行注释与 /* */ 块注释）; 引号内注释不处理（本层只做守卫够用）。"""
    text = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text)
    return text


class DuckDBQuery:
    """DuckDB 只读查询封装（内存连接; 3.10）。"""

    def __init__(self) -> None:
        import duckdb

        self._con = duckdb.connect()  # 内存库, 只读查询用
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._con.close()
            self._closed = True

    # ------------------------------------------------------------------
    # 只读安全化（3.10: 禁 DDL/DML, mtzquant sql 仅允许 SELECT）
    # ------------------------------------------------------------------
    @staticmethod
    def assert_select_only(sql: str) -> str:
        """校验语句只读; 返回去注释、去尾分号的单条语句（违反抛 MtzQuantError）。"""
        from mtzquant.core.errors import MtzQuantError

        text = _strip_sql_comment(sql).strip().rstrip(";").strip()
        if not text:
            raise MtzQuantError("SQL 语句为空", stage="duckdb_query")
        parts = [p for p in re.split(r";", text) if p.strip()]
        if len(parts) != 1:
            raise MtzQuantError(
                "mtzquant sql 仅允许单条只读语句",
                stage="duckdb_query",
                hint="一次一条 SELECT/WITH; 写面操作一律拒绝（3.10）",
            )
        lowered = parts[0].lower()
        if not lowered.startswith(_SELECT_PREFIXES):
            raise MtzQuantError(
                "仅允许只读语句（SELECT/WITH/SHOW/DESCRIBE/EXPLAIN）",
                stage="duckdb_query",
                hint="写面/副作用语句一律拒绝（3.10）",
            )
        tokens = re.findall(r"[a-z_]+", lowered)
        for bad in _FORBIDDEN_KEYWORDS:
            if bad in tokens:
                raise MtzQuantError(
                    f"SQL 含写面/副作用关键字 {bad!r}, 已拒绝（3.10 只读安全化）",
                    stage="duckdb_query",
                    hint="数据写入走 DataFetcher/原子落盘, 不经 SQL",
                )
        return parts[0]

    def execute_select(self, sql: str) -> Any:
        """执行只读查询并返回 pandas DataFrame（读失败抛结构化错误）。"""
        from mtzquant.core.errors import MtzQuantError

        stmt = self.assert_select_only(sql)
        if self._closed:
            raise MtzQuantError("DuckDBQuery 已关闭", stage="duckdb_query")
        try:
            return self._con.execute(stmt).df()
        except Exception as exc:  # noqa: BLE001
            raise MtzQuantError(
                f"DuckDB 查询失败: {type(exc).__name__}: {exc}",
                stage="duckdb_query",
                hint="检查 SQL 与 CSV 列名（tushare 源格式 ts_code/trade_date/OHLC/vol/amount）",
            ) from exc

    # ------------------------------------------------------------------
    # read_csv_auto 直读（3.10）
    # ------------------------------------------------------------------
    def read_csv_auto(self, path: str | Path, *, filename: bool = True) -> Any:
        """直读单文件或 glob 目录（filename=true 带 _file 列, 跨品种扫描）。"""
        p = str(path).replace("\\", "/")
        return self.execute_select(
            "SELECT * FROM read_csv_auto("
            f"'{p}', filename={str(filename).lower()}, header=true, sample_size=100000)"
        )

    def read_kline(self, path: str | Path) -> Any:
        """读 K 线 CSV（tushare 源格式列子集, 3.10 体检用）。"""
        p = str(path).replace("\\", "/")
        return self.execute_select(
            "SELECT trade_date, open, high, low, close, vol, amount "
            f"FROM read_csv_auto('{p}', header=true, sample_size=100000)"
        )

    # ------------------------------------------------------------------
    # 数据质量体检（3.10）
    # ------------------------------------------------------------------
    def quality_report(self, path: str | Path) -> dict[str, Any]:
        """对单文件/glob 跑全部体检模板; 返回 {check: DataFrame}（空表=通过）。"""
        p = str(path).replace("\\", "/")
        out: dict[str, Any] = {}
        for name, template in QUALITY_CHECKS.items():
            try:
                out[name] = self.execute_select(template.format(path=p))
            except Exception:  # noqa: BLE001
                # 体检单项失败不阻断整体（记录原因, 由上层决定）
                out[name] = None
        return out
