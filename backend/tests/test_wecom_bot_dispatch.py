"""wecom_bot 渠道: 告警分发 + 规则白名单。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.settings import router
from app.services import preferences
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules


class CaptureExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


def _engine(channels):
    return type("Engine", (), {"rules": {"r1": {"webhook_channels": channels}}})()


def _event():
    return {"rule_id": "r1", "source": "price", "symbol": "600000.SH",
            "name": "浦发银行", "message": "突破 10.00"}


def _setup(monkeypatch, tmp_path, alert_chat):
    p = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: p)
    preferences._invalidate_cache()
    monkeypatch.setattr(preferences, "get_feishu_webhook_url", lambda: "")
    monkeypatch.setattr(preferences, "get_wecom_webhook_url", lambda: "")
    if alert_chat:
        preferences.set_wecom_bot_alert_chat(alert_chat, 2)
    ex = CaptureExecutor()
    import app.services.quote_service as qs_mod
    monkeypatch.setattr(qs_mod, "_WEBHOOK_EXECUTOR", ex)
    return ex


class _BotSvc:
    def send_markdown(self, chatid, chat_type, content):
        return True


def _qs_with_bot():
    qs = object.__new__(QuoteService)
    qs._app_state = type("S", (), {"wecom_bot_service": _BotSvc()})()
    return qs


def test_dispatch_wecom_bot_enqueued(monkeypatch, tmp_path):
    ex = _setup(monkeypatch, tmp_path, alert_chat="groupXYZ789")
    QuoteService._maybe_send_webhook(_qs_with_bot(), [_event()], _engine(["wecom_bot"]))
    assert len(ex.calls) == 1
    _, args = ex.calls[0]
    assert args[0] == "groupXYZ789" and args[1] == 2
    assert "**价格**" in args[2] and "600000.SH" in args[2]


def test_dispatch_no_alert_chat_skips(monkeypatch, tmp_path):
    ex = _setup(monkeypatch, tmp_path, alert_chat=None)
    QuoteService._maybe_send_webhook(_qs_with_bot(), [_event()], _engine(["wecom_bot"]))
    assert ex.calls == []


def test_dispatch_channel_not_selected_skips(monkeypatch, tmp_path):
    ex = _setup(monkeypatch, tmp_path, alert_chat="groupXYZ789")
    QuoteService._maybe_send_webhook(_qs_with_bot(), [_event()], _engine(["feishu"]))
    assert ex.calls == []


def test_rule_whitelist_keeps_wecom_bot():
    rule = monitor_rules.normalize({"webhook_channels": ["wecom_bot", "bogus"]})
    assert rule["webhook_channels"] == ["wecom_bot"]


def test_legacy_rule_migration_unchanged():
    rule = monitor_rules.normalize({"webhook_enabled": True})
    assert rule["webhook_channels"] == ["feishu", "wecom"]


# ===== Task 4: settings API =====


@pytest.fixture()
def prefs_client(monkeypatch, tmp_path):
    """monkeypatch preferences 到 tmp 文件后构造 TestClient(app)。

    参照 test_minute_refresh.py:436-446 的模式: FastAPI() + include_router(router) + TestClient,
    无鉴权中间件拦截 (settings router 无 Depends 鉴权)。
    """
    p = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: p)
    preferences._invalidate_cache()
    app = FastAPI()
    app.include_router(router)
    # webhook-test wecom_bot 分支会读 app.state.wecom_bot_service, 设为 None 模拟未配置
    app.state.wecom_bot_service = None
    yield TestClient(app)
    preferences._invalidate_cache()


def test_alert_chat_endpoint_rejects_unknown(prefs_client):
    r = prefs_client.put("/api/settings/preferences/wecom-bot-alert-chat",
                         json={"chatid": "nope", "chat_type": 2})
    assert r.status_code == 400


def test_alert_chat_endpoint_roundtrip_and_clear(prefs_client):
    from app.services import preferences as prefs
    prefs.register_wecom_bot_chat("groupXYZ789", 2)
    r = prefs_client.put("/api/settings/preferences/wecom-bot-alert-chat",
                         json={"chatid": "groupXYZ789"})
    assert r.status_code == 200 and r.json()["wecom_bot_alert_chat"]["chatid"] == "groupXYZ789"
    r = prefs_client.put("/api/settings/preferences/wecom-bot-alert-chat", json={"chatid": ""})
    assert r.json()["wecom_bot_alert_chat"] == {}


def test_webhook_test_wecom_bot_no_chat(prefs_client):
    r = prefs_client.post("/api/settings/preferences/webhook-test", json={"channel": "wecom_bot"})
    assert r.status_code == 200 and r.json()["ok"] is False
