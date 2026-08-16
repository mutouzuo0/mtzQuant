# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/16 10:05:00
# @description : 结构化异常体系（设计 10.5 / 4.9）; M2-K7 NotImplementedApiError 补机读字段

"""结构化异常体系（设计 10.5 / 4.9）。

所有框架错误继承 MtzQuantError，携带结构化字段（可用 to_dict() 机读）:
    run_id   关联回测运行（可为 None：加载/配置阶段尚无 run）
    stage    出错阶段标识（normalize_code / config / adapter:joinquant ...）
    hint     修复建议（面向用户与 AI 编码助手）
未实现平台 API 抛 NotImplementedApiError —— 结构化报错而非静默失败（设计 4.9）。
"""

from __future__ import annotations

from typing import Any


class MtzQuantError(Exception):
    """mtzQuant 结构化异常基类。"""

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        stage: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.message = message
        self.run_id = run_id
        self.stage = stage
        self.hint = hint
        super().__init__(self._format())

    def _format(self) -> str:
        parts = ["[mtzquant] " + self.message]
        if self.stage:
            parts.append(f"  阶段: {self.stage}")
        if self.hint:
            parts.append(f"  建议: {self.hint}")
        if self.run_id:
            parts.append(f"  run_id: {self.run_id}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """机读输出（CLI --json / AI 解析）。"""
        return {
            "type": type(self).__name__,
            "message": self.message,
            "run_id": self.run_id,
            "stage": self.stage,
            "hint": self.hint,
        }


class InvalidCodeError(MtzQuantError):
    """证券代码无法归一（设计 3.4）。"""

    def __init__(self, raw: str, reason: str) -> None:
        super().__init__(
            f"无法识别的证券代码: {raw!r}（{reason}）",
            stage="normalize_code",
            hint=(
                "支持的输入形式: 600000.SH / 600000.XSHG / 600000.SS / sh600000 / "
                "1.600000；内部统一后缀 .SH(沪) .SZ(深) .BJ(北交所)"
            ),
        )


class NotImplementedApiError(MtzQuantError):
    """策略平台 API 未实现（设计 4.9：非静默失败，附替代建议与实现指引）。"""

    def __init__(
        self,
        api_name: str,
        platform: str,
        level: str = "L2",
        alternative: str | None = None,
    ) -> None:
        self.api_name = api_name  # M2-K7: 机读字段（4.9 模板）
        self.platform = platform
        self.level = level
        self.alternative = alternative
        hint = f"兼容清单: docs/compat/{platform}.md"
        if alternative:
            hint += f"；可选替代: {alternative}"
        hint += "；如需实现: 参考「适配器开发指南」#新增API"
        super().__init__(
            f"API `{api_name}` 暂未实现({level})",
            stage=f"adapter:{platform}",
            hint=hint,
        )


class ConfigError(MtzQuantError):
    """配置加载 / 校验失败（配置文件视为开发约定而非运行期输入）。"""

    def __init__(self, message: str) -> None:
        super().__init__(
            f"配置错误: {message}",
            stage="config",
            hint="对照 config/settings.example.json 检查字段名与取值",
        )
