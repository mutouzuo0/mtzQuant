# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 15:00:00
# @update_time        : 2026/09/05 21:10:00
# @description : O4 akshare 源驱动（RemoteKlineSource）：ETF/股票/指数日线 + 主数据尽力

"""akshare 源驱动（设计 3.2/3.9）——`RemoteKlineSource` 实现。

- 日线: ETF `fund_etf_hist_em` / 股票 `stock_zh_a_hist`（中文列 → tushare 源格式）;
- 主数据（尽力, 3.11）: ETF `fund_etf_spot_em` / 股票 `stock_info_a_code_name`
  （仅 code/name; 全字段主数据归 M3）;
- 可选依赖（download 组）; 未安装/网络异常由上层 retry/fallback 处理。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from mtzquant.core.codes import normalize_code
from mtzquant.data.drivers.remote import register_source
from mtzquant.data.drivers.tushare_driver import TUSHARE_KLINE_COLS

_ZH_COLUMN_MAP = {
    "日期": "trade_date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "vol",
    "成交额": "amount",
}


class AkshareSource:
    """akshare 行情/主数据源（RemoteKlineSource, 1 次/秒限流）。"""

    name = "akshare"

    def __init__(self, *, fetch_fn=None) -> None:
        self._fetch_fn = fetch_fn  # 测试注入（绕过真实网络）

    # ------------------------------------------------------------------
    def _api(self):
        import akshare as ak  # 可选依赖（extras=[download]）

        return ak

    def fetch_kline(
        self, code: str, start: date, end: date, *, instrument_type: str
    ) -> pd.DataFrame:
        if self._fetch_fn is not None:
            return self._fetch_fn(code, start, end)
        ak = self._api()
        symbol = normalize_code(code).split(".")[0]
        if instrument_type == "etf":
            df = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            )
        elif instrument_type == "index":
            df = ak.index_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
        else:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            )
        if df is None or df.empty:
            return pd.DataFrame(columns=list(TUSHARE_KLINE_COLS))
        df = df.rename(columns=_ZH_COLUMN_MAP)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
        df.insert(0, "ts_code", normalize_code(code))
        return df[[c for c in TUSHARE_KLINE_COLS if c in df.columns]]

    def fetch_master(self, instrument_type: str | None = None) -> pd.DataFrame:
        ak = self._api()
        if instrument_type == "etf":
            spot = ak.fund_etf_spot_em()
            return spot.rename(columns={"代码": "ts_code", "名称": "name"})[["ts_code", "name"]]
        names = ak.stock_info_a_code_name()
        return names.rename(columns={"code": "ts_code", "name": "name"})[["ts_code", "name"]]


register_source("akshare", AkshareSource)
