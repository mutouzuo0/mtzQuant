# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 14:58:00
# @update_time        : 2026/09/05 21:10:00
# @description : O4 tushare 源驱动：pro.daily/fund_daily/index_daily 日线 + 主数据

"""tushare 源驱动（设计 3.2/3.9）——`RemoteKlineSource` 实现。

- 日线: 股票 `pro.daily` / ETF `pro.fund_daily`（返回 tushare 源格式列）;
- 主数据: 股票 `pro.stock_basic` / 基金 `pro.fund_basic`;
- token 优先级: `MTZQUANT_TUSHARE_TOKEN` 环境变量 > secrets.json `tushare.token`（3.6）;
- 可选依赖（download 组）; 未安装/网络异常由上层 retry/fallback 处理。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError
from mtzquant.data.drivers.remote import register_source

# tushare 源格式列（3.5; 落盘保持此「归一前原始」列序）
TUSHARE_KLINE_COLS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")


class TushareSource:
    """tushare 行情/主数据源（RemoteKlineSource）。"""

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
                stage="fetch_tushare",
                hint="MTZQUANT_TUSHARE_TOKEN 环境变量 或 secrets.json 的 tushare.token（3.6）",
            )
        import tushare as ts  # 可选依赖（extras=[download]）

        self._pro = ts.pro_api(token)
        return self._pro

    def fetch_kline(
        self, code: str, start: date, end: date, *, instrument_type: str
    ) -> pd.DataFrame:
        pro = self._api()
        api_name = {"etf": "fund_daily", "index": "index_daily"}.get(instrument_type, "daily")
        fn = getattr(pro, api_name, None)
        if fn is None:
            raise MtzQuantError(
                f"tushare 接口 {api_name} 不可用", stage="fetch_tushare", hint="检查 tushare 版本"
            )
        df = fn(
            ts_code=normalize_code(code),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=list(TUSHARE_KLINE_COLS))
        cols = [c for c in TUSHARE_KLINE_COLS if c in df.columns]
        return df[cols]

    def fetch_master(self, instrument_type: str | None = None) -> pd.DataFrame:
        pro = self._api()
        if instrument_type == "etf":
            df = pro.fund_basic(market="E")
        else:
            # 股票主数据: L(上市)+D(已退市) 全量——支撑 get_all_securities PIT 历史成分
            frames: list[pd.DataFrame] = []
            for status in ("L", "D"):
                part = pro.stock_basic(
                    exchange="",
                    list_status=status,
                    fields=("ts_code,symbol,name,area,industry,market,list_date,delist_date"),
                )
                if part is not None and not part.empty:
                    frames.append(part)
            df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return df


register_source("tushare", TushareSource)
