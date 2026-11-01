"""配置管理属性测试。"""

import os
import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st
from pydantic import ValidationError


# Feature: sequoia-x-v2, Property 1: 环境变量覆盖配置默认值
@given(db_path=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/_.-")))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_env_overrides_default(db_path: str, monkeypatch) -> None:
    """属性 1：任意合法 db_path 通过环境变量设置后，Settings 实例应反映该值。"""
    import sequoia_x.core.config as cfg_module
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(cfg_module, "_settings", None)
    from sequoia_x.core.config import Settings
    s = Settings()
    assert s.db_path == db_path


# Feature: sequoia-x-v2, Property 2: 缺失 feishu_webhook_url 时降级为本地报告
def test_missing_webhook_degrades_to_local_report() -> None:
    """未配置 feishu_webhook_url 时不再抛错，feishu_enabled 为 False（走本地报告）。"""
    import os
    from sequoia_x.core.config import Settings
    # 确保环境变量中没有该字段
    env_backup = os.environ.pop("FEISHU_WEBHOOK_URL", None)
    try:
        s = Settings(_env_file=None)
        assert s.feishu_webhook_url == ""
        assert s.feishu_enabled is False
    finally:
        if env_backup is not None:
            os.environ["FEISHU_WEBHOOK_URL"] = env_backup


# Feature: sequoia-x-v2, Property 3: 占位符 Webhook 视为未配置
def test_placeholder_webhook_is_not_usable() -> None:
    """模板占位符或非法 URL 不算可用 Webhook。"""
    from sequoia_x.core.config import Settings

    assert Settings.is_usable_webhook("") is False
    assert Settings.is_usable_webhook(None) is False
    assert Settings.is_usable_webhook("https://open.feishu.cn/open-apis/bot/v2/hook/your-default-token") is False
    assert Settings.is_usable_webhook("https://example.com/hook") is False
    assert Settings.is_usable_webhook("https://open.feishu.cn/open-apis/bot/v2/hook/abc123") is True
