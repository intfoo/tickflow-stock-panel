"""企业微信智能机器人长连接推送渠道 — preferences 注册表/目标 + WecomBotService 收发。"""
import asyncio
import json

import pytest

from app.services import preferences
from app.services.wecom_bot_service import WecomBotService


@pytest.fixture()
def prefs_tmp(tmp_path, monkeypatch):
    p = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: p)
    monkeypatch.setattr(preferences, "_register_last_write", 0.0)  # 重置注册表写盘节流, 防测试间串扰
    preferences._invalidate_cache()
    yield p
    preferences._invalidate_cache()


# ===== Task 1: preferences =====

def test_register_and_get_chats(prefs_tmp):
    preferences.register_wecom_bot_chat("chatABC123", 2)
    preferences.register_wecom_bot_chat("user01", 1)
    chats = preferences.get_wecom_bot_chats()
    assert {c["chatid"] for c in chats} == {"chatABC123", "user01"}
    g = next(c for c in chats if c["chatid"] == "chatABC123")
    assert g["chat_type"] == 2 and g["label"].startswith("群聊")
    s = next(c for c in chats if c["chatid"] == "user01")
    assert s["chat_type"] == 1 and "user01" in s["label"]


def test_register_invalid_ignored(prefs_tmp):
    preferences.register_wecom_bot_chat("", 2)
    preferences.register_wecom_bot_chat("x", 0)
    assert preferences.get_wecom_bot_chats() == []


def test_register_throttle_existing(prefs_tmp, monkeypatch):
    preferences.register_wecom_bot_chat("chatABC123", 2)
    monkeypatch.setattr(preferences, "_register_last_write", __import__("time").time())
    preferences.register_wecom_bot_chat("chatABC123", 2)  # 节流窗口内 → 不写盘
    assert len(preferences.get_wecom_bot_chats()) == 1


def test_alert_chat_roundtrip(prefs_tmp):
    assert preferences.get_wecom_bot_alert_chat() == {}
    preferences.set_wecom_bot_alert_chat("chatABC123", 2)
    assert preferences.get_wecom_bot_alert_chat() == {"chatid": "chatABC123", "chat_type": 2}
    preferences.set_wecom_bot_alert_chat("")
    assert preferences.get_wecom_bot_alert_chat() == {}


def test_webhook_default_channels_allows_wecom_bot(prefs_tmp):
    preferences.set_webhook_default_channels(["feishu", "wecom_bot", "bogus"])
    assert preferences.get_webhook_default_channels() == ["feishu", "wecom_bot"]


# ===== Task 2: WecomBotService 帧分发 / 会话注册 / 发送 =====

def _svc():
    return WecomBotService()


def test_dispatch_resolves_pending_future(prefs_tmp):
    svc = _svc()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    svc._pending["r1"] = fut
    svc._dispatch_frame(json.dumps({"headers": {"req_id": "r1"}, "errcode": 0, "errmsg": "ok"}))
    assert fut.done() and fut.result()["errcode"] == 0
    assert "r1" not in svc._pending
    loop.close()


def test_dispatch_pong_falls_through_to_handler(prefs_tmp):
    svc = _svc()
    # 无 cmd、req_id 不在 pending → 走 _handle_incoming, 不炸
    svc._dispatch_frame(json.dumps({"headers": {}, "errcode": 0}))
    svc._dispatch_frame(json.dumps({"cmd": "ping"}))


def test_register_chat_from_group_msg(prefs_tmp):
    svc = _svc()
    svc._dispatch_frame(json.dumps({
        "cmd": "aibot_msg_callback",
        "body": {"chatid": "groupXYZ789", "chattype": "group", "msgtype": "text",
                 "from": {"userid": "user01"}, "text": {"content": "hi"}},
    }))
    chats = preferences.get_wecom_bot_chats()
    assert chats[0]["chatid"] == "groupXYZ789" and chats[0]["chat_type"] == 2


def test_register_chat_from_single_msg_and_event(prefs_tmp):
    svc = _svc()
    svc._dispatch_frame(json.dumps({
        "cmd": "aibot_msg_callback",
        "body": {"chattype": "single", "msgtype": "text",
                 "from": {"userid": "user02"}, "text": {"content": "hi"}},
    }))
    svc._dispatch_frame(json.dumps({
        "cmd": "aibot_event_callback",
        "body": {"from": {"userid": "user03"}, "event": {"eventtype": "enter_chat"}},
    }))
    ids = {c["chatid"] for c in preferences.get_wecom_bot_chats()}
    assert {"user02", "user03"} <= ids


def test_send_markdown_not_connected(prefs_tmp):
    assert _svc().send_markdown("c1", 2, "hello") is False


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def test_send_frame_format_and_response(prefs_tmp):
    svc = _svc()
    ws = _FakeWS()

    async def run():
        svc._ws = ws
        task = asyncio.ensure_future(svc._send_frame(
            {"cmd": "aibot_send_msg", "headers": {"req_id": "req-9"}, "body": {}}
        ))
        await asyncio.sleep(0)  # 让 send 先执行
        svc._dispatch_frame(json.dumps({"headers": {"req_id": "req-9"}, "errcode": 0}))
        return await task

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    resp = loop.run_until_complete(run())
    assert resp["errcode"] == 0
    assert ws.sent[0]["cmd"] == "aibot_send_msg"
    assert ws.sent[0]["headers"]["req_id"] == "req-9"
    loop.close()


def test_send_frame_ws_unavailable(prefs_tmp):
    svc = _svc()
    with pytest.raises(RuntimeError):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(svc._send_frame(
                {"cmd": "aibot_send_msg", "headers": {"req_id": "r"}, "body": {}}
            ))
        finally:
            loop.close()
