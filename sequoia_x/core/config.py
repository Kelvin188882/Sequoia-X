"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v2.db"
    start_date: str = "2024-01-01"
    # 飞书 Webhook 改为可选：未配置时自动降级为本地报告输出（reports/ 目录）
    feishu_webhook_url: str = ""
    strategy_webhooks: dict[str, str] = {}
    # 本地报告输出目录（无飞书 Webhook 时生效）
    report_dir: str = "reports"
    # baostock 并发数：过高会被判为滥用并临时拉黑 IP，生产环境建议 2~4
    sync_workers: int = 4
    backfill_workers: int = 2

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
    )

    def model_post_init(self, __context: object) -> None:
        """初始化后合并 STRATEGY_WEBHOOK_ 前缀的环境变量到 strategy_webhooks。"""
        import os

        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = dict(self.strategy_webhooks)
        for key, value in os.environ.items():
            if key.upper().startswith(prefix):
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的 Webhook URL。

        优先从 strategy_webhooks 查找，找不到则 fallback 到 feishu_webhook_url。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'breakout'。

        Returns:
            对应的 Webhook URL 字符串。
        """
        return self.strategy_webhooks.get(webhook_key.lower(), self.feishu_webhook_url)

    @staticmethod
    def is_usable_webhook(url: str | None) -> bool:
        """判断 Webhook 是否为真实可用的地址（排除空值和模板占位符）。"""
        if not url:
            return False
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return False
        placeholders = ("your-", "your_", "example.com", "xxx", "placeholder")
        return not any(p in url.lower() for p in placeholders)

    def get_usable_webhook(self, webhook_key: str) -> str | None:
        """返回可用的 Webhook URL；策略专属未配置时回退到默认地址。"""
        for candidate in (self.strategy_webhooks.get(webhook_key.lower()), self.feishu_webhook_url):
            if self.is_usable_webhook(candidate):
                return candidate  # type: ignore[return-value]
        return None

    @property
    def feishu_enabled(self) -> bool:
        """是否存在任一可用的飞书 Webhook（默认地址或策略专属地址）。

        只配置策略专属地址而未配默认地址时也应走飞书通道——此前仅检查
        default，会导致专属 Webhook 被静默忽略、结果只写本地报告。
        某个策略没有可用地址时，由 FeishuNotifier 单独降级，不影响其他策略。
        """
        if self.is_usable_webhook(self.feishu_webhook_url):
            return True
        return any(self.is_usable_webhook(url) for url in self.strategy_webhooks.values())


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时从环境变量或 .env 文件加载配置。所有字段均有默认值，
    未配置飞书 Webhook 时自动降级为本地报告输出。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 当字段类型不匹配（如 SYNC_WORKERS 非整数）时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
