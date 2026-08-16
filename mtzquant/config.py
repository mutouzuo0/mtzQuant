# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 21:33:45
# @update_time        : 2026/08/16 21:59:08
# @description : 配置加载与校验（设计 3.6 配置体系）

"""配置加载与校验（设计 3.6 配置体系）。

三层配置与安全纪律:
    config/settings.json   框架级非敏感配置（本地文件，不入 Git）
    config/secrets.json    密钥类配置（本地文件，不入 Git，.gitignore 强制排除）
    *.example.json         空值/默认值模板（入库，克隆后复制填写）

加载优先级: 环境变量（MTZQUANT_*）> secrets.json > settings.json。
敏感字段（token/api_key/secret/webhook/password）永不写入 settings.json 与任务 JSON。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# 敏感字段名模式（入库脱敏用，设计 3.6/8.3.1）
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = ("token", "api_key", "secret", "webhook", "password")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_SECRETS_PATH = PROJECT_ROOT / "config" / "secrets.json"


# ------------------------------------------------------------------
# pydantic 模型（字段与 config/settings.example.json 一一对应）
# ------------------------------------------------------------------
class LocalCsvSettings(BaseModel):
    """本地 CSV 数据源路径与解析规则（设计 3.6/3.12）。"""

    root_path: str = "./data"
    kline_day_dir: str = "kline/{type}/day"
    kline_minute_dir: str = "kline/{type}/minute"
    master_dir: str = "master"
    factor_dir: str = "factor/adj_factor/{type}"
    corporate_actions_dir: str = "corporate_actions/{type}"
    calendar_dir: str = "calendars"
    constituents_dir: str = "index_constituents"
    fundamentals_dir: str = "fundamentals"  # M3-R2: fundamentals/{table}/{code}.csv
    raw_dir: str = "raw"
    group_by_type: bool = True
    file_pattern: str = "{code}.csv"
    minute_shard: str = "{code}/{YYYY-MM}.parquet"
    keep_raw: bool = False
    encoding: str = "utf-8-sig"
    format: str = "auto"  # auto=tushare|joinquant|generic 嗅探
    adjust: str = "none"  # none|forward|backward，仅指标研究用（3.14）


class TushareSettings(BaseModel):
    max_retry: int = 3


class DownloadSettings(BaseModel):
    """下载限流与去重策略（设计 3.9）。"""

    rate_limit_per_min: int = 60
    akshare_min_interval_sec: float = 1.0
    batch_days: int = 365
    dedup_keep: str = "latest"  # latest|first


class CacheSettings(BaseModel):
    """冷热分离与 parquet 二级缓存（设计 3.7）。"""

    enabled: bool = True
    parquet_dir: str = ".cache/parquet"
    preload_mode: str = "window"  # window|all|lazy
    warmup_bars_default: int = 120


class DataSettings(BaseModel):
    driver: str = "local_csv"
    local_csv: LocalCsvSettings = Field(default_factory=LocalCsvSettings)
    tushare: TushareSettings = Field(default_factory=TushareSettings)
    download: DownloadSettings = Field(default_factory=DownloadSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)


class DatabaseSettings(BaseModel):
    url: str = "sqlite:///./mtzquant.db"
    echo: bool = False
    batch_size: int = 500
    batch_flush_interval_ms: int = 500
    buffer_max_rows: int = 50000


class SlippageSettings(BaseModel):
    type: str = "ratio"  # ratio|fixed
    value: float = 0.001


class FeeSettings(BaseModel):
    commission_rate: float = 0.0001
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001


class EngineSettings(BaseModel):
    fill_price: str = "next_open"  # next_open|same_close|next_close（设计 5.3.3）
    slippage: SlippageSettings = Field(default_factory=SlippageSettings)
    default_fees: FeeSettings = Field(default_factory=FeeSettings)
    max_participation: float = 0.25  # 单笔 ≤ bar 成量比例（PTrade set_volume_ratio 默认一致）
    random_seed: int = 42  # 确定性复现（设计 8.8）


class ResearchSettings(BaseModel):
    """研究层默认（M3 §5）: 因子计算预热窗口 / 摩擦归因近似。"""

    warmup_default: int = 60  # 因子计算最少回看根数（动量/波动窗口, S1）
    bps_default: float = 10.0  # 向量化回测费用近似（S4, 万分之?）——0.001 = 10bp


class OptimizerSettings(BaseModel):
    """参数扫描默认（M3 §5, 10.4）。"""

    max_workers: int = 4  # BacktestQueue 并行 worker 数（D2）
    mode_defaults: tuple[str, ...] = ("grid", "random")  # bayesian 预留（2.2）


class Settings(BaseModel):
    data: DataSettings = Field(default_factory=DataSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    optimizer: OptimizerSettings = Field(default_factory=OptimizerSettings)


# ------------------------------------------------------------------
# 加载函数
# ------------------------------------------------------------------
def load_settings(path: Path | str | None = None) -> Settings:
    """加载 settings.json；文件缺失时返回默认值（本地零配置可跑）。

    路径优先级: 显式 path > 环境变量 MTZQUANT_SETTINGS > config/settings.json（3.6:
    环境变量优先于文件）。子进程（--isolate）通过 MTZQUANT_SETTINGS 传递隔离配置。
    """
    if path is None:
        env_path = os.environ.get("MTZQUANT_SETTINGS")
        path = Path(env_path) if env_path else DEFAULT_SETTINGS_PATH
    path = Path(path)
    if not path.exists():
        return Settings()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Settings.model_validate(raw)


def load_secrets(path: Path | str | None = None) -> dict[str, Any]:
    """加载 secrets.json（若存在）。密钥永不打印、永不入日志。"""
    path = Path(path) if path is not None else DEFAULT_SECRETS_PATH
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def get_tushare_token(secrets: dict[str, Any] | None = None) -> str | None:
    """tushare token 读取优先级: 环境变量 MTZQUANT_TUSHARE_TOKEN > secrets.json。"""
    env = os.environ.get("MTZQUANT_TUSHARE_TOKEN")
    if env:
        return env
    if secrets is None:
        secrets = load_secrets()
    token = secrets.get("tushare", {}).get("token")
    return token or None


def sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """params_json 入库前脱敏：命中敏感字段名的键置空（设计 3.6/8.3.1）。"""

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: ("" if any(p in k.lower() for p in SENSITIVE_KEY_PATTERNS) else _walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_walk(i) for i in obj]
        return obj

    return _walk(params)
