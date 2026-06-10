"""Unit tests for src/max_bridge_bot.py routing logic.

No network: MAX API is replaced with a stub, send_to_tg/resolve_chat are
recorded fakes, bridge_store works on a temporary sqlite file.
"""
import asyncio
import time

import pytest

from src import bridge_store
from src import max_bridge_bot as mbb

ADMIN_ID = 111
ADMIN_CHAT = 222


class FakeMaxAPI:
    """Records send_message calls; returns incrementing mids."""

    def __init__(self):
        self.sent: list[tuple[int | None, int | None, str]] = []
        self._mid = 0

    async def send_message(self, chat_id=None, user_id=None, text="", notify=True, attachments=None):
        self.sent.append((chat_id, user_id, text))
        self._mid += 1
        return {"message": {"body": {"mid": f"mid.{self._mid}"}}}


class FakeSendToTg:
    def __init__(self, fail=False):
        self.calls: list[tuple[int, str, int | None]] = []
        self.fail = fail

    async def __call__(self, tg_chat_id, text, reply_to_msg_id):
        if self.fail:
            raise RuntimeError("telethon down")
        self.calls.append((tg_chat_id, text, reply_to_msg_id))


async def _resolve_yolo(query):
    if "yolo" in query.lower():
        return (-100123, "YOLO 2.0")
    return None


def make_update(text, user_id=ADMIN_ID, chat_id=ADMIN_CHAT, reply_mid=None):
    msg = {
        "sender": {"user_id": user_id},
        "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
        "body": {"mid": "mid.in", "text": text},
    }
    if reply_mid:
        msg["link"] = {"type": "reply", "message": {"mid": reply_mid}}
    return {"update_type": "message_created", "message": msg}


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    """Temp DB for bridge_store, admin id, and unconfigured callbacks."""
    if bridge_store._conn is not None:
        bridge_store._conn.close()
    bridge_store._conn = None
    monkeypatch.setattr(bridge_store, "DB_PATH", tmp_path / "bridge.db")
    monkeypatch.setattr(mbb, "MAX_ADMIN_USER_ID", ADMIN_ID)
    monkeypatch.setattr(mbb, "_send_to_tg", None)
    monkeypatch.setattr(mbb, "_resolve_chat", None)
    yield
    if bridge_store._conn is not None:
        bridge_store._conn.close()
    bridge_store._conn = None


def handle(api, update, send_to_tg, resolve_chat=_resolve_yolo) -> bool:
    mbb.configure(send_to_tg, resolve_chat)
    return asyncio.run(mbb.handle_bridge_update(api, update))


# --- reply routing ---

def test_reply_with_mapping_routes_to_tg():
    bridge_store.save_mapping("mid.42", -100555, 777, "Agents Bali")
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("ok, deal", reply_mid="mid.42"), tg)

    assert consumed is True
    assert tg.calls == [(-100555, "ok, deal", 777)]
    # delivery confirmation back in MAX mentions the chat title
    assert len(api.sent) == 1
    assert api.sent[0][0] == ADMIN_CHAT
    assert "Agents Bali" in api.sent[0][2]


def test_reply_without_mapping_falls_through_to_claude():
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("hello", reply_mid="mid.unknown"), tg)

    assert consumed is False
    assert tg.calls == []
    assert api.sent == []


def test_reply_mid_extracted_from_linked_body():
    bridge_store.save_mapping("mid.9", -100777, 5, "T")
    api, tg = FakeMaxAPI(), FakeSendToTg()
    upd = make_update("hi")
    upd["message"]["link"] = {"type": "reply", "message": {"body": {"mid": "mid.9"}}}

    consumed = handle(api, upd, tg)

    assert consumed is True
    assert tg.calls == [(-100777, "hi", 5)]


def test_send_to_tg_failure_reported_in_max():
    bridge_store.save_mapping("mid.42", -100555, 777, "Agents Bali")
    api, tg = FakeMaxAPI(), FakeSendToTg(fail=True)

    consumed = handle(api, make_update("text", reply_mid="mid.42"), tg)

    assert consumed is True
    assert len(api.sent) == 1
    assert "Ошибка" in api.sent[0][2]
    assert "отправлено" not in api.sent[0][2]


# --- plain text stays with Claude ---

def test_plain_text_not_consumed():
    bridge_store.set_active_chat(-100123, "YOLO 2.0")
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("question for claude"), tg)

    assert consumed is False
    assert tg.calls == []
    assert api.sent == []


# --- commands ---

def test_send_command_goes_to_active_chat():
    bridge_store.set_active_chat(-100123, "YOLO 2.0")
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("/send status update"), tg)

    assert consumed is True
    assert tg.calls == [(-100123, "status update", None)]
    assert "YOLO 2.0" in api.sent[0][2]


def test_send_command_without_active_chat_hints():
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("/send orphan text"), tg)

    assert consumed is True
    assert tg.calls == []
    assert "/chat" in api.sent[0][2]


def test_send_command_without_text_shows_usage():
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("/send"), tg)

    assert consumed is True
    assert "Использование" in api.sent[0][2]


def test_chat_command_sets_active_chat():
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("/chat yolo"), tg)

    assert consumed is True
    assert bridge_store.get_active_chat() == (-100123, "YOLO 2.0")
    assert "YOLO 2.0" in api.sent[0][2]


def test_chat_command_not_found():
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("/chat nosuchchat"), tg)

    assert consumed is True
    assert bridge_store.get_active_chat() is None
    assert "не найден" in api.sent[0][2]


def test_chat_command_without_query_shows_usage():
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("/chat"), tg)

    assert consumed is True
    assert "Использование" in api.sent[0][2]


def test_status_reports_active_chat_and_daily_count():
    bridge_store.set_active_chat(-100123, "YOLO 2.0")
    bridge_store.save_mapping("mid.1", -100123, 1, "YOLO 2.0")
    bridge_store.save_mapping("mid.2", -100123, 2, "YOLO 2.0")
    # old record outside the 24h window must not be counted
    bridge_store.save_mapping("mid.old", -100123, 3, "YOLO 2.0")
    with bridge_store._lock:
        bridge_store._get_conn().execute(
            "UPDATE bridge_map SET ts = ? WHERE max_msg_id = 'mid.old'",
            (int(time.time()) - 2 * 86400,),
        )
        bridge_store._conn.commit()
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("/status"), tg)

    assert consumed is True
    text = api.sent[0][2]
    assert "YOLO 2.0" in text
    assert "2" in text


def test_foreign_commands_not_consumed():
    api, tg = FakeMaxAPI(), FakeSendToTg()

    for cmd in ("/clear", "/help", "/reset"):
        consumed = handle(api, make_update(cmd), tg)
        assert consumed is False, cmd

    assert api.sent == []


# --- filtering ---

def test_non_admin_messages_not_consumed():
    bridge_store.save_mapping("mid.42", -100555, 777, "Agents Bali")
    api, tg = FakeMaxAPI(), FakeSendToTg()

    consumed = handle(api, make_update("spy text", user_id=999, reply_mid="mid.42"), tg)

    assert consumed is False
    assert tg.calls == []
    assert api.sent == []


def test_unconfigured_bridge_consumes_nothing():
    bridge_store.save_mapping("mid.42", -100555, 777, "Agents Bali")
    api = FakeMaxAPI()

    consumed = asyncio.run(
        mbb.handle_bridge_update(api, make_update("text", reply_mid="mid.42"))
    )

    assert consumed is False
    assert api.sent == []


# --- notify_max ---

def test_notify_max_returns_mid(monkeypatch):
    api = FakeMaxAPI()
    monkeypatch.setattr(mbb, "_api", api)
    monkeypatch.setattr(mbb, "MAX_ADMIN_USER_ID", ADMIN_ID)

    mid = asyncio.run(mbb.notify_max("forwarded message", {"chat": "YOLO 2.0"}))

    assert mid == "mid.1"
    # sent by user_id (dialog), not chat_id
    assert api.sent == [(None, ADMIN_ID, "forwarded message")]


def test_notify_max_splits_long_text_and_returns_last_mid(monkeypatch):
    api = FakeMaxAPI()
    monkeypatch.setattr(mbb, "_api", api)
    monkeypatch.setattr(mbb, "MAX_ADMIN_USER_ID", ADMIN_ID)

    mid = asyncio.run(mbb.notify_max("x" * 4500, {}))

    assert len(api.sent) == 2
    assert len(api.sent[0][2]) == 4000
    assert mid == "mid.2"
