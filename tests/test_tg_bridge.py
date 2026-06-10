"""Unit tests for src/tg_bridge.py: filtering, formatting, anti-flood.

No network, no real Telethon objects — events and senders are mocked
with SimpleNamespace.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tg_bridge import (
    FloodBatcher,
    build_batch_text,
    classify_media,
    format_message,
    media_placeholder,
    parse_whitelist,
    should_forward,
)


def make_event(out=False, is_private=False, is_group=False, is_channel=False, chat_id=0):
    return SimpleNamespace(out=out, is_private=is_private, is_group=is_group,
                           is_channel=is_channel, chat_id=chat_id)


def make_sender(bot=False):
    return SimpleNamespace(bot=bot)


# --- parse_whitelist ---

def test_parse_whitelist_basic():
    assert parse_whitelist("123, -456,789") == {123, -456, 789}


def test_parse_whitelist_empty_and_junk():
    assert parse_whitelist("") == set()
    assert parse_whitelist(" , abc, 42") == {42}


# --- should_forward ---

def test_private_dialog_forwarded():
    event = make_event(is_private=True, chat_id=111)
    assert should_forward(event, make_sender(), set()) is True


def test_private_bot_ignored():
    event = make_event(is_private=True, chat_id=111)
    assert should_forward(event, make_sender(bot=True), set()) is False


def test_private_no_sender_ignored():
    event = make_event(is_private=True, chat_id=111)
    assert should_forward(event, None, set()) is False


def test_outgoing_ignored():
    event = make_event(out=True, is_private=True, chat_id=111)
    assert should_forward(event, make_sender(), set()) is False


def test_group_whitelisted_forwarded():
    event = make_event(is_group=True, chat_id=-100123)
    assert should_forward(event, make_sender(), {-100123}) is True


def test_group_not_whitelisted_ignored():
    event = make_event(is_group=True, chat_id=-100999)
    assert should_forward(event, make_sender(), {-100123}) is False


def test_supergroup_in_whitelist():
    # Telethon supergroups: is_group=True AND is_channel=True
    event = make_event(is_group=True, is_channel=True, chat_id=-100123)
    assert should_forward(event, make_sender(), {-100123}) is True


def test_broadcast_channel_ignored():
    event = make_event(is_channel=True, chat_id=-100777)
    assert should_forward(event, make_sender(), {-100777}) is False


# --- media_placeholder ---

def make_message(photo=None, voice=None, document=None, file=None, media=None):
    return SimpleNamespace(photo=photo, voice=voice, document=document,
                           file=file, media=media)


def test_placeholder_photo():
    assert media_placeholder(make_message(photo=object())) == "[фото]"


def test_placeholder_voice():
    assert media_placeholder(make_message(voice=object())) == "[голосовое]"


def test_placeholder_document_with_name():
    msg = make_message(document=object(), file=SimpleNamespace(name="report.pdf"))
    assert media_placeholder(msg) == "[документ: report.pdf]"


def test_placeholder_document_without_name():
    msg = make_message(document=object(), file=SimpleNamespace(name=None))
    assert media_placeholder(msg) == "[документ]"


def test_placeholder_none_for_text():
    assert media_placeholder(make_message()) is None


# --- classify_media ---

def make_media_msg(**kwargs):
    base = dict(photo=None, voice=None, video_note=None, video=None,
                audio=None, document=None, file=None, media=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_classify_photo():
    assert classify_media(make_media_msg(photo=object())) == ("image", "photo.jpg")


def test_classify_voice():
    assert classify_media(make_media_msg(voice=object())) == ("audio", "voice.ogg")


def test_classify_document_named():
    msg = make_media_msg(document=object(), file=SimpleNamespace(name="a.pdf", size=10))
    assert classify_media(msg) == ("file", "a.pdf")


def test_classify_video():
    assert classify_media(make_media_msg(video=object())) == ("video", "video.mp4")


def test_classify_text_none():
    assert classify_media(make_media_msg()) is None


# --- format_message ---

def test_format_plain_text():
    assert format_message("Anton", "hello") == "[Anton]\nhello"


def test_format_media_with_caption():
    assert format_message("Anton", "look", "[фото]") == "[Anton]\n[фото]\nlook"


def test_format_media_without_caption():
    assert format_message("Anton", "", "[голосовое]") == "[Anton]\n[голосовое]"


# --- build_batch_text ---

def entry(text, chat_title="Chat", tg_chat_id=1, tg_msg_id=1):
    return {"text": text, "chat_title": chat_title,
            "tg_chat_id": tg_chat_id, "tg_msg_id": tg_msg_id}


def test_batch_text_single_passthrough():
    assert build_batch_text([entry("[Chat]\nhi")]) == "[Chat]\nhi"


def test_batch_text_combines_and_strips_headers():
    text = build_batch_text([entry("[Chat]\none"), entry("[Chat]\ntwo")])
    assert text == "[Chat] (2 messages)\n- one\n- two"


# --- FloodBatcher ---

def test_flood_batching():
    async def scenario():
        sent = []

        async def send(entries):
            sent.append(entries)

        batcher = FloodBatcher(send, window=0.05, threshold=5)
        for i in range(7):
            await batcher.submit(1, entry(f"m{i}", tg_msg_id=i))

        # First 5 within threshold go out individually, 6th and 7th buffered.
        assert len(sent) == 5
        await asyncio.sleep(0.1)
        assert len(sent) == 6
        assert [e["text"] for e in sent[-1]] == ["m5", "m6"]

    asyncio.run(scenario())


def test_no_batching_below_threshold():
    async def scenario():
        sent = []

        async def send(entries):
            sent.append(entries)

        batcher = FloodBatcher(send, window=0.05, threshold=5)
        for i in range(3):
            await batcher.submit(1, entry(f"m{i}"))
        assert len(sent) == 3
        await asyncio.sleep(0.1)
        assert len(sent) == 3

    asyncio.run(scenario())


def test_flood_chats_independent():
    async def scenario():
        sent = []

        async def send(entries):
            sent.append(entries)

        batcher = FloodBatcher(send, window=0.05, threshold=2)
        # Chat 1 floods (3 > 2), chat 2 stays normal.
        for i in range(3):
            await batcher.submit(1, entry(f"a{i}", tg_chat_id=1))
        await batcher.submit(2, entry("b0", chat_title="Other", tg_chat_id=2))
        assert len(sent) == 3  # a0, a1 individually + b0
        await asyncio.sleep(0.1)
        assert len(sent) == 4
        assert [e["text"] for e in sent[-1]] == ["a2"]

    asyncio.run(scenario())
