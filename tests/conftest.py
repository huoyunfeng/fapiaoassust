"""pytest 全局配置：统一隔离会污染测试的本机环境状态。"""
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _isolate_dev_mode(monkeypatch):
    """强制关闭开发者模式。

    开发机 config.json 常置 dev_mode=true，会让 license_service.plan_key()
    直接返回 enterprise（全功能放开、不限额度），导致所有免费版拦截与授权
    断言失效。需要专门测试 dev_mode 行为的用例自行 monkeypatch 覆盖此默认值。
    """
    monkeypatch.setattr(settings, "dev_mode", False)
