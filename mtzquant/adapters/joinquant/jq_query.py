# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 10:40:00
# @update_time        : 2026/08/17 10:40:00
# @description : M3 聚宽 query DSL：query/filter/order_by +
#   valuation/indicator/finance.STK_XR_XD 求值（PIT）

"""聚宽 query DSL（M3, 设计 4.6/3.13）——`get_fundamentals` / `finance.run_query` 的求值引擎。

对象:
  - `Column`: 表列引用（valuation.code / indicator.inc_net_profit_year_on_year /
    finance.STK_XR_XD.a_registration_date）; 支持算术(`/`)、比较(`>= <= > < ==`)、
    `.in_(list)`、`.asc()/.desc()`;
  - `Expr`: 列间算术表达式（如 valuation.pe_ratio / indicator.inc_net_profit_year_on_year）;
  - `Cond`: 过滤条件; `Query`: `query(*fields).filter(*conds).order_by(col.asc())`。

求值（`eval_query`）:
  1. 按涉表（valuation→daily_basic, indicator→fina_indicator, finance.STK_XR_XD→dividend）;
  2. 从 filter 收集 code.in_(list) 作为查询池（缺省=universe）;
  3. 每 code 经 `provider.fundamentals(code, table, fields, as_of, knowledge_time)` 读 PIT 行;
     valuation/indicator 取 as_of 最新一行, dividend 取全行;
  4. valuation/indicator 按 code 联结 → 条件过滤 → 按请求字段投影 → 排序。

字段映射（jq 字段 → 本地基本面表列）:
  valuation.market_cap = daily_basic.total_mv × 1e4（tushare 万元 → 元）
  valuation.pe_ratio   = daily_basic.pe
  indicator.inc_net_profit_year_on_year = fina_indicator.netprofit_yoy
  finance.STK_XR_XD.a_registration_date = dividend.record_date（股权登记日）
  finance.STK_XR_XD.bonus_amount_rmb    = dividend.cash_div_tax（每股派息税前, 元）
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from mtzquant.adapters.shared.code_style import denormalize_code
from mtzquant.core.codes import normalize_code

_SSE = "Asia/Shanghai"


def _asof(d: date | datetime) -> datetime:
    """查询日 → 当日 15:00 Asia/Shanghai（PIT 截止, 盘后可见当日披露）。"""
    if isinstance(d, datetime):
        d = d.date()
    return datetime(d.year, d.month, d.day, 15, 0, tzinfo=_tz())


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(_SSE)


# ----------------------------------------------------------------------
# DSL 对象
# ----------------------------------------------------------------------
class Column:
    """表列引用（可出现在 query 字段/条件/排序）。"""

    def __init__(self, table_key: str, field: str, source_col: str, scale: float = 1.0) -> None:
        self.table_key = table_key
        self.field = field  # jq 字段名（结果列名）
        self.source_col = source_col  # 本地基本面表列
        self.scale = scale

    def _op(self, op: str, other: Any) -> Cond:
        return Cond(self, op, other)

    def __ge__(self, other: Any) -> Cond:
        return self._op(">=", other)

    def __le__(self, other: Any) -> Cond:
        return self._op("<=", other)

    def __gt__(self, other: Any) -> Cond:
        return self._op(">", other)

    def __lt__(self, other: Any) -> Cond:
        return self._op("<", other)

    def __eq__(self, other: Any) -> Cond:  # type: ignore[override]
        return self._op("==", other)

    def __ne__(self, other: Any) -> Cond:  # type: ignore[override]
        return self._op("!=", other)

    def in_(self, values: list[str]) -> Cond:
        return Cond(self, "in", list(values))

    def __truediv__(self, other: Any) -> Expr:
        return Expr("/", self, other)

    def __mul__(self, other: Any) -> Expr:
        return Expr("*", self, other)

    def asc(self) -> OrderSpec:
        return OrderSpec(self, "asc")

    def desc(self) -> OrderSpec:
        return OrderSpec(self, "desc")

    def __repr__(self) -> str:
        return f"{self.table_key}.{self.field}"


class Expr:
    """列算术表达式（求值时按行计算）。"""

    def __init__(self, op: str, left: Any, right: Any) -> None:
        self.op = op
        self.left = left
        self.right = right

    def _op(self, op: str, other: Any) -> Cond:
        return Cond(self, op, other)

    def __gt__(self, other: Any) -> Cond:
        return self._op(">", other)

    def __lt__(self, other: Any) -> Cond:
        return self._op("<", other)

    def __ge__(self, other: Any) -> Cond:
        return self._op(">=", other)

    def __le__(self, other: Any) -> Cond:
        return self._op("<=", other)

    def __eq__(self, other: Any) -> Cond:  # type: ignore[override]
        return self._op("==", other)

    def __repr__(self) -> str:
        return f"({self.left} {self.op} {self.right})"


class Cond:
    """过滤条件（op ∈ >=,<=,>,<,==,!=,in）。"""

    def __init__(self, expr: Any, op: str, value: Any) -> None:
        self.expr = expr
        self.op = op
        self.value = value


class OrderSpec:
    def __init__(self, column: Column, direction: str) -> None:
        self.column = column
        self.direction = direction


class Query:
    def __init__(self, *fields: Any) -> None:
        self.fields = list(fields)
        self.conds: list[Cond] = []
        self.order: list[OrderSpec] = []

    def filter(self, *conds: Cond) -> Query:
        self.conds.extend(conds)
        return self

    def order_by(self, *specs: OrderSpec) -> Query:
        self.order.extend(specs)
        return self


def query(*fields: Any) -> Query:
    """聚宽 query 入口。"""
    return Query(*fields)


# ----------------------------------------------------------------------
# 表定义（jq 表 → 本地基本面表 + 字段映射）
# ----------------------------------------------------------------------
# 每字段: 源列名 或 (源列名, 缩放)
_TABLES: dict[str, dict[str, tuple[str, float] | str]] = {
    "valuation": {
        "code": "ts_code",
        "market_cap": ("total_mv", 1e4),  # tushare 万元 → 元
        "pe_ratio": "pe",
        "pb_ratio": "pb",
    },
    "indicator": {
        "code": "ts_code",
        "inc_net_profit_year_on_year": "netprofit_yoy",
        "inc_revenue_year_on_year": "or_yoy",
    },
    "finance.STK_XR_XD": {
        "code": "ts_code",
        "a_registration_date": "record_date",
        "bonus_amount_rmb": "cash_div_tax",
        "ex_date": "ex_date",
    },
}

# 每 jq 表 → 本地基本面表名（provider.fundamentals 的 table 参数）
_TABLE_TO_LOCAL = {
    "valuation": "daily_basic",
    "indicator": "fina_indicator",
    "finance.STK_XR_XD": "dividend",
}


class _Table:
    """表对象: `valuation.code` / `indicator.inc_net_profit_year_on_year` 属性访问。"""

    def __init__(self, table_key: str) -> None:
        self._key = table_key

    def __getattr__(self, name: str) -> Column:
        fields = _TABLES[self._key]
        if name not in fields:
            raise AttributeError(f"{self._key} 无字段 {name!r}（可选: {sorted(fields)}）")
        spec = fields[name]
        if isinstance(spec, tuple):
            source_col, scale = spec
        else:
            source_col, scale = spec, 1.0
        return Column(self._key, name, source_col, scale)

    def __repr__(self) -> str:
        return f"<jq table {self._key}>"


class _Finance:
    STK_XR_XD = _Table("finance.STK_XR_XD")


valuation = _Table("valuation")
indicator = _Table("indicator")
finance = _Finance()


# ----------------------------------------------------------------------
# 求值
# ----------------------------------------------------------------------
def _tables_of(query: Query) -> set[str]:
    """查询涉及的 jq 表（字段 + 条件表达式）。"""
    out: set[str] = set()
    for f in query.fields:
        out.update(_expr_tables(f))
    for c in query.conds:
        out.update(_expr_tables(c.expr))
        out.update(_expr_tables(c.value))
    for s in query.order:
        out.add(s.column.table_key)
    return out


def _expr_tables(node: Any) -> set[str]:
    if isinstance(node, Column):
        return {node.table_key}
    if isinstance(node, Expr):
        return _expr_tables(node.left) | _expr_tables(node.right)
    return set()


def _collect_codes(query: Query) -> list[str] | None:
    """从条件里收集 code.in_(list)（取第一个; 无则 None → 全 universe）。"""
    for c in query.conds:
        if isinstance(c.expr, Column) and c.expr.field == "code" and c.op == "in":
            return [normalize_code(x) for x in c.value]
    return None


def _read_row(
    ctx: Any, code: str, local_table: str, source_cols: list[str], as_of: datetime
) -> pd.DataFrame | None:
    """读单 code 单表 PIT 可见行（provider.fundamentals）。"""
    provider = getattr(ctx, "provider", None)
    if provider is None:
        return None
    fn = getattr(provider, "fundamentals", None)
    if fn is None:
        return None
    try:
        return fn(code, local_table, source_cols, as_of=as_of, knowledge_time=as_of)
    except Exception:  # noqa: BLE001 - 缺数据/缺披露日 → 该 code 无该表值
        return None


def _latest_value(frame: pd.DataFrame | None, source_col: str, scale: float) -> float | None:
    """取 as_of 最新一行的列值（valuation/indicator 语义: 最新披露/最新交易日）。"""
    if frame is None or frame.empty or source_col not in frame.columns:
        return None
    val = pd.to_numeric(frame[source_col], errors="coerce").iloc[-1]
    if pd.isna(val):
        return None
    return float(val) * scale


def _row_values(
    ctx: Any, code: str, local_table: str, as_of: datetime
) -> dict[str, float | str | None]:
    """valuation/indicator: 每 jq 字段的 as_of 最新值（行键按 jq 字段名）。"""
    key = _key_of(local_table)
    fields = _TABLES[key]
    source_cols = sorted({f[0] if isinstance(f, tuple) else f for f in fields.values()})
    frame = _read_row(ctx, code, local_table, source_cols, as_of)
    out: dict[str, float | str | None] = {"__code__": code}
    if frame is None or frame.empty:
        return out
    for jq_field, fspec in fields.items():
        if jq_field == "code":
            out[jq_field] = code
            continue
        src, scale = fspec if isinstance(fspec, tuple) else (fspec, 1.0)
        out[jq_field] = _latest_value(frame, src, scale)
    return out


def _key_of(local_table: str) -> str:
    for jq_key, loc in _TABLE_TO_LOCAL.items():
        if loc == local_table:
            return jq_key
    return local_table


def _eval_cond_value(node: Any, row: dict[str, Any], as_of: datetime) -> Any:
    if isinstance(node, Column):
        return row.get(node.field)
    if isinstance(node, Expr):
        left = _eval_cond_value(node.left, row, as_of)
        right = _eval_cond_value(node.right, row, as_of)
        if left is None or right is None or right == 0:
            return None
        if node.op == "/":
            return left / right
        if node.op == "*":
            return left * right
    if isinstance(node, datetime) or isinstance(node, date):
        return datetime(node.year, node.month, node.day)
    return node


def _match_cond(cond: Cond, row: dict[str, Any], as_of: datetime) -> bool:
    """条件判定（None 值一律不通过, 对齐 jq 缺数据行过滤语义）。"""
    if isinstance(cond.expr, Column) and cond.expr.field == "code":
        # code.in_ 已用于池收集 → 恒过; code== 按 __code__ 判定
        if cond.op == "in":
            return True
        value = row.get("__code__")
        return value == cond.value
    value = _eval_cond_value(cond.expr, row, as_of)
    if value is None:
        return False
    target = cond.value
    # 日期/时间戳比较归一（date vs datetime）
    if isinstance(value, (datetime, date)) or isinstance(target, (datetime, date)):
        v = value.date() if isinstance(value, datetime) else value
        t = target.date() if isinstance(target, datetime) else target
        value, target = v, t
    if cond.op == "in":
        return target is not None and (
            value in target
            if not isinstance(value, (list, tuple))
            else bool(set(value) & set(target))
        )
    try:
        if cond.op == ">=":
            return value >= target
        if cond.op == "<=":
            return value <= target
        if cond.op == ">":
            return value > target
        if cond.op == "<":
            return value < target
        if cond.op == "==":
            return value == target
        if cond.op == "!=":
            return value != target
    except TypeError:
        return False
    return False


def _project(row: dict[str, Any], field: Any, as_of: datetime) -> Any:
    """投影单个请求字段 → 值（Column/Expr）。

    日期字段输出为 ISO 字符串——对齐聚宽 `finance.run_query` 的日期列类型:
    原版策略会对结果 `groupby('code').sum()`（只取数值列分红, 日期列被字符串拼接忽略）。
    """
    if isinstance(field, Column):
        value = row.get(field.field)
        if isinstance(value, date) and not isinstance(value, datetime):
            return value.isoformat()
        return value
    if isinstance(field, Expr):
        return _eval_cond_value(field, row, as_of)
    return field


def eval_query(ctx: Any, q: Query, as_of: date | datetime) -> pd.DataFrame:
    """求值 query → jq 语义 DataFrame（列=请求字段名; code 列归一码）。"""
    asof = _asof(as_of)
    tables = _tables_of(q)
    codes = _collect_codes(q)
    if codes is None:
        universe_fn = getattr(ctx, "universe_fn", None)
        codes = list(universe_fn()) if universe_fn is not None else []

    # dividend（finance.STK_XR_XD）特殊: 单表多行（每分红事件一行）
    dividend_table = "finance.STK_XR_XD" in tables
    rows: list[dict[str, Any]] = []
    if dividend_table:
        for code in sorted(codes):
            frame = _read_row(
                ctx,
                code,
                "dividend",
                ["record_date", "ann_date", "cash_div_tax", "ex_date"],
                asof,
            )
            if frame is None or frame.empty:
                continue
            for _, r in frame.iterrows():
                rows.append(
                    {
                        "__code__": code,
                        "code": code,
                        "a_registration_date": _parse_date(r.get("record_date")),
                        "bonus_amount_rmb": _num(r.get("cash_div_tax")),
                        "ex_date": _parse_date(r.get("ex_date")),
                    }
                )
    else:
        local_tables = sorted({_TABLE_TO_LOCAL[t] for t in tables})
        for code in sorted(codes):
            row: dict[str, Any] = {"__code__": code}
            for loc in local_tables:
                row.update(_row_values(ctx, code, loc, asof))
            rows.append(row)

    # 条件过滤
    rows = rows if not dividend_table else rows
    out = []
    for row in rows:
        if all(_match_cond(c, row, asof) for c in q.conds):
            out.append(row)
    col_names = [f.field if isinstance(f, Column) else repr(f) for f in q.fields]
    if not out:
        return pd.DataFrame(columns=col_names)

    # 投影（列名: Column→字段名, Expr→表达式串）
    data = {
        name: [_project(r, f, asof) for r in out]
        for name, f in zip(col_names, q.fields, strict=True)
    }
    df = pd.DataFrame(data)
    # 平台码统一: code 列输出为聚宽码（600000.XSHG）——与 get_all_securities/
    # get_current_data/positions 一致
    if "code" in df.columns:
        df["code"] = [denormalize_code(c) for c in df["code"]]

    # 排序
    for spec in reversed(q.order):
        col = spec.column.field
        if col in df.columns:
            df = df.sort_values(col, ascending=(spec.direction == "asc"), kind="stable")
    return df.reset_index(drop=True)


def _parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    text = str(v).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    text = str(v).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    try:
        return float(text)
    except ValueError:
        return None
