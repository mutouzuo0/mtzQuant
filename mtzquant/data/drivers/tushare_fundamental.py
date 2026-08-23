# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 23:06:00
# @update_time        : 2026/08/16 23:06:00
# @description : M3-R1 tushare 基本面源：fina_indicator/daily_basic/成分快照（三字段 PIT）

"""tushare 基本面与成分源驱动（设计 3.13 PIT 四时间, M3-R1）——`RemoteFundamentalSource` 实现。

- 财务指标最小集: `pro.fina_indicator`（netprofit_yoy/or_yoy, 按 ann_date 区间拉取）;
- 每日估值最小集: `pro.daily_basic`（pe/pb/total_mv/circ_mv, 按 trade_date 拉取）;
- 指数成分快照:  `pro.index_weight`（某交易日成分+权重）+ `pro.index_member`（入/出日期区间）。

三字段 PIT 化（设计 3.13）——**落盘保持源原始列**（ts_code/ann_date/end_date/trade_date）,
event_time/published_at 的语义映射由 `FundamentalsStore` 在查询期统一完成（R3）:
  fina_indicator: event_time=end_date(报告期) / published_at=ann_date(披露日)
  daily_basic:    event_time=trade_date / published_at=trade_date
available_at 默认=published_at, 供应商同步延迟用 `FundamentalsStore(delay_offset_days)` 可配偏移。

token 优先级同行情源: `MTZQUANT_TUSHARE_TOKEN` > secrets.json `tushare.token`（3.6）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError
from mtzquant.data.drivers.remote import register_fundamental_source

# 源原始列（M3-R2 落盘保持此列序; 读时由 FundamentalsStore 解析）
FINA_INDICATOR_COLS = ("ts_code", "ann_date", "end_date", "netprofit_yoy", "or_yoy")
DAILY_BASIC_COLS = ("ts_code", "trade_date", "pe", "pb", "total_mv", "circ_mv")
DIVIDEND_COLS = ("ts_code", "ann_date", "record_date", "ex_date", "div_proc", "cash_div_tax")
CONSTITUENT_COLS = ("index_code", "con_code", "in_date", "out_date", "weight")


class TushareFundamentalSource:
    """tushare 基本面/成分源（RemoteFundamentalSource）。"""

    name = "tushare"

    def __init__(
        self,
        *,
        token: str | None = None,
        secrets: dict[str, Any] | None = None,
        pro: Any = None,
    ) -> None:
        self._token = token
        self._secrets = secrets
        self._pro = pro  # 测试注入（绕过真实网络）

    # ------------------------------------------------------------------
    def _api(self) -> Any:
        if self._pro is not None:
            return self._pro
        from mtzquant.config import get_tushare_token

        token = self._token or get_tushare_token(self._secrets)
        if not token:
            raise MtzQuantError(
                "tushare token 缺失",
                stage="fetch_tushare_fundamental",
                hint="MTZQUANT_TUSHARE_TOKEN 环境变量 或 secrets.json 的 tushare.token（3.6）",
            )
        import tushare as ts  # 可选依赖（extras=[download]）

        self._pro = ts.pro_api(token)
        return self._pro

    # ------------------------------------------------------------------
    def fetch_fina_indicator(self, code: str, start: date, end: date) -> pd.DataFrame:
        """财务指标（fina_indicator, 按 ann_date 区间; 保留最新修订 update_flag=1）。

        tushare 对同一 (报告期, 公告日) 会返回 update_flag 0/1 两行（值相同, 新旧标记）,
        只保留 1（最新）避免 `_clean_fund` 重复主键拒绝; 不同 ann_date 的修订版本行照常保留。
        """
        pro = self._api()
        fn = getattr(pro, "fina_indicator", None)
        if fn is None:
            raise MtzQuantError(
                "tushare fina_indicator 接口不可用",
                stage="fetch_tushare_fundamental",
                hint="检查 tushare 版本与积分权限（3.13）",
            )
        df = fn(
            ts_code=normalize_code(code),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            fields="ts_code,ann_date,end_date,netprofit_yoy,or_yoy,update_flag",
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=list(FINA_INDICATOR_COLS))
        if "update_flag" in df.columns:
            df = df[df["update_flag"].astype(str) == "1"]
        cols = [c for c in FINA_INDICATOR_COLS if c in df.columns]
        return df[cols]

    def fetch_daily_basic(self, code: str, start: date, end: date) -> pd.DataFrame:
        """每日估值（daily_basic, 按 trade_date 区间）。"""
        pro = self._api()
        fn = getattr(pro, "daily_basic", None)
        if fn is None:
            raise MtzQuantError(
                "tushare daily_basic 接口不可用",
                stage="fetch_tushare_fundamental",
                hint="检查 tushare 版本与积分权限（3.13）",
            )
        df = fn(
            ts_code=normalize_code(code),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=list(DAILY_BASIC_COLS))
        cols = [c for c in DAILY_BASIC_COLS if c in df.columns]
        return df[cols]

    def fetch_dividend(self, code: str, start: date, end: date) -> pd.DataFrame:
        """分红送配（dividend, 全历史; 仅保留「实施中」记录——对齐聚宽 STK_XR_XD）。

        聚宽 `finance.STK_XR_XD` 只含已实施除权除息的记录（股权登记日/派息金额）,
        预案/未实施不进入该表; tushare `pro.dividend` 返回全部分红公告, 故按
        `div_proc`（实施进度）过滤。start/end 仅作幂等粗判的区间语义, 源全量返回。
        """
        pro = self._api()
        fn = getattr(pro, "dividend", None)
        if fn is None:
            raise MtzQuantError(
                "tushare dividend 接口不可用",
                stage="fetch_tushare_fundamental",
                hint="检查 tushare 版本与积分权限（3.13 分红表）",
            )
        df = fn(ts_code=normalize_code(code))
        if df is None or df.empty:
            return pd.DataFrame(columns=list(DIVIDEND_COLS))
        cols = [c for c in DIVIDEND_COLS if c in df.columns]
        out = df[cols].copy()
        if "div_proc" in out.columns:
            out = out[out["div_proc"].astype(str).str.contains("实施", na=False)]
        # 去除缺失/非法 公告日/登记日 行（STK_XR_XD 需 a_registration_date=record_date,
        # 缺则不可 PIT）
        for c in ("ann_date", "record_date"):
            out = out[out[c].astype(str).str.match(r"^\d{8}$", na=False)]
        return out.reset_index(drop=True)

    def fetch_index_constituents(self, index_code: str, trade_date: date) -> pd.DataFrame:
        """指数成分快照: index_weight（该交易日成分+权重）+ index_member（入/出日期区间）。"""
        pro = self._api()
        ymd = trade_date.strftime("%Y%m%d")
        w = getattr(pro, "index_weight", None)
        if w is None:
            raise MtzQuantError(
                "tushare index_weight 接口不可用",
                stage="fetch_tushare_fundamental",
                hint="检查 tushare 版本与积分权限（3.13 成分快照）",
            )
        norm = normalize_code(index_code)
        df = w(index_code=norm, start_date=ymd, end_date=ymd)
        if df is None or df.empty:
            return pd.DataFrame(columns=list(CONSTITUENT_COLS))
        # 权重/成分（该交易日全部成分; index_weight 返回 trade_date 粒度全成分+weight）
        members = df[df["trade_date"] == ymd].copy()
        if "con_code" not in members.columns:
            members["con_code"] = members.get("ts_code", "")
        if "weight" not in members.columns:
            members["weight"] = float("nan")
        # 入/出日期区间（index_member 全历史; 缺失则不填——快照仍可作 PIT 池）
        try:
            m = getattr(pro, "index_member", None)
            if m is not None:
                hist = m(index_code=norm)
                if hist is not None and not hist.empty:
                    member_map = dict(
                        zip(
                            hist["con_code"].astype(str),
                            zip(
                                hist.get("in_date", ""),
                                hist.get("out_date", ""),
                                strict=False,
                            ),
                            strict=True,
                        )
                    )
                    members["in_date"] = members["con_code"].map(
                        lambda c: member_map.get(str(c), ("", ""))[0]  # type: ignore[union-attr]
                    )
                    members["out_date"] = members["con_code"].map(
                        lambda c: member_map.get(str(c), ("", ""))[1]  # type: ignore[union-attr]
                    )
                else:
                    members["in_date"] = ""
                    members["out_date"] = ""
            else:
                members["in_date"] = ""
                members["out_date"] = ""
        except Exception:  # noqa: BLE001 - index_member 属增强信息, 缺失不阻断快照
            members["in_date"] = ""
            members["out_date"] = ""
        out = pd.DataFrame(
            {
                "index_code": norm,
                "con_code": members["con_code"].astype(str),
                "in_date": members["in_date"].astype(str),
                "out_date": members["out_date"].astype(str),
                "weight": pd.to_numeric(members["weight"], errors="coerce"),
            }
        )
        return out.drop_duplicates(subset=["con_code"]).reset_index(drop=True)


register_fundamental_source("tushare", TushareFundamentalSource)
