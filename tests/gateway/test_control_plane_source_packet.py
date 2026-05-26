import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.control_plane_source_packet import (
    build_control_plane_task_body,
    write_control_plane_source_packet,
)
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner, _control_plane_offload_reason
from gateway.session import SessionSource


def _event(text: str, *, platform=Platform.DISCORD, **kwargs) -> MessageEvent:
    source = SessionSource(
        platform=platform,
        chat_id="chat-1",
        thread_id="thread-1",
        chat_type="thread",
        user_id="user-1",
        user_name="Nic",
    )
    return MessageEvent(text=text, source=source, message_id="msg-1", **kwargs)


def test_write_source_packet_bounds_excerpt_and_redacts_secret(tmp_path):
    secret = "sk-testsecret1234567890abcdef"
    text = f"build the app and fix the frontend issue with token={secret}\n" + ("detail " * 900)
    event = _event(text, reply_to_text=f"prior secret {secret}")

    packet = write_control_plane_source_packet(
        event,
        reason="control_plane_work_request",
        active_excerpt_chars=120,
        hermes_home=tmp_path / "hermes",
        wiki_home=tmp_path / "wiki",
    )

    raw_path = Path(packet["raw_path"])
    wiki_path = Path(packet["wiki_path"])
    assert raw_path.exists()
    assert wiki_path.exists()
    assert stat_mode(raw_path) == "0o600"
    assert len(packet["excerpt"]) == 120
    assert packet["omitted_chars"] > 0

    raw_text = raw_path.read_text()
    raw = json.loads(raw_text)
    assert raw["kind"] == "gateway_control_plane_source_packet"
    assert raw["request"]["text"] != text
    assert secret not in raw_text
    assert secret not in wiki_path.read_text()
    assert "Raw local packet" in wiki_path.read_text()

    body = build_control_plane_task_body(event, reason="control_plane_work_request", packet=packet)
    assert "Durable context pointers" in body
    assert str(raw_path) in body
    assert str(wiki_path) in body
    assert secret not in body
    assert "chars omitted from active task packet" in body


def test_control_plane_short_status_bypasses_offload():
    assert _control_plane_offload_reason("status please", {"control_plane": {"enabled": True}}) is None


def test_control_plane_kanban_body_uses_source_packet_for_large_requests(monkeypatch, tmp_path):
    captured = {}

    class Conn:
        def close(self):
            pass

    def fake_create_task(conn, **kwargs):
        captured.update(kwargs)
        return "t_packet"

    import hermes_cli.kanban_db as kanban_db

    monkeypatch.setattr(kanban_db, "connect", lambda: Conn())
    monkeypatch.setattr(kanban_db, "create_task", fake_create_task)
    monkeypatch.setattr(kanban_db, "add_notify_sub", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.control_plane_source_packet.get_hermes_home", lambda: tmp_path / "hermes")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    runner = object.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "default"
    runner._active_profile_name = MagicMock(return_value="app-builder")

    long_text = "build the app feature " + ("implementation detail " * 500)
    event = _event(long_text)
    task_id, assignee = runner._create_control_plane_kanban_task(
        event=event,
        reason="control_plane_work_request",
        user_config={"control_plane": {"default_assignee": "app-builder"}},
    )

    assert task_id == "t_packet"
    assert assignee == "app-builder"
    body = captured["body"]
    assert "Raw source packet" in body
    assert "Original request:" not in body
    assert len(body) < len(long_text)
    raw_path = body.split("Raw source packet (0600, redacted full request/context): `", 1)[1].split("`", 1)[0]
    assert Path(raw_path).exists()


def stat_mode(path: Path) -> str:
    return oct(os.stat(path).st_mode & 0o777)
