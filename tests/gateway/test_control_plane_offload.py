import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import (
    GatewayRunner,
    _control_plane_inline_wall_timeout,
    _control_plane_offload_reason,
    _control_plane_platform_enabled,
)
from gateway.session import SessionSource


def _source(platform=Platform.DISCORD):
    return SessionSource(
        platform=platform,
        chat_id="chat-1",
        thread_id="thread-1",
        user_id="user-1",
        user_name="Nic",
        chat_type="dm",
    )


def _event(text, platform=Platform.DISCORD):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(platform),
        message_id="msg-1",
    )


def test_control_plane_short_status_stays_inline():
    cfg = {"control_plane": {"enabled": True, "platforms": ["discord"]}}
    assert _control_plane_platform_enabled(_source(), cfg) is True
    assert _control_plane_offload_reason("status", cfg) is None
    assert _control_plane_offload_reason("ping", cfg) is None
    assert _control_plane_offload_reason("fix bug", cfg) is None


def test_control_plane_build_request_is_offloaded():
    cfg = {"control_plane": {"enabled": True, "platforms": ["discord"]}}
    reason = _control_plane_offload_reason(
        "fix the AIVS generation runtime and verify the app build", cfg
    )
    assert reason == "control_plane_work_request"


def test_control_plane_document_handoff_request_is_offloaded_before_wall_timeout():
    cfg = {"control_plane": {"enabled": True, "platforms": ["discord"]}}
    reason = _control_plane_offload_reason(
        "Is there anything over the stages that was missed? Then document all for a new handoff",
        cfg,
    )
    assert reason == "control_plane_work_request"


def test_control_plane_disabled_or_non_control_platform_stays_inline():
    assert _control_plane_platform_enabled(
        _source(Platform.TELEGRAM),
        {"control_plane": {"enabled": True, "platforms": ["discord"]}},
    ) is False
    assert _control_plane_platform_enabled(
        _source(),
        {"control_plane": {"enabled": False, "platforms": ["discord"]}},
    ) is False


def test_control_plane_inline_wall_timeout_is_scoped_to_control_platforms():
    cfg = {
        "control_plane": {
            "enabled": True,
            "platforms": ["discord"],
            "inline_wall_timeout_seconds": 12,
        }
    }
    assert _control_plane_inline_wall_timeout(_source(), cfg) == 12
    assert _control_plane_inline_wall_timeout(_source(Platform.TELEGRAM), cfg) is None


@pytest.mark.asyncio
async def test_control_plane_offload_returns_task_ack_without_running_agent(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._busy_ack_ts = {}
    runner._session_run_generation = {}
    runner._context_spill_locks = {}
    runner._update_prompt_pending = {}
    runner._draining = False
    runner._kanban_notifier_profile = "gateway"
    runner._session_db = None
    runner._active_profile_name = lambda: "app-builder"
    runner._is_user_authorized = lambda source: True
    runner._is_telegram_topic_root_lobby = lambda source: False

    async def fail_agent(*args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("agent path should not run for offloaded work")

    runner._handle_message_with_agent = fail_agent
    runner._create_control_plane_kanban_task = lambda **kwargs: ("t_abc12345", "app-builder")

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"control_plane": {"enabled": True, "platforms": ["discord"]}},
    )
    monkeypatch.setattr("tools.clarify_gateway.get_pending_for_session", lambda key: None)
    monkeypatch.setattr("tools.slash_confirm.get_pending", lambda key: None)
    monkeypatch.setattr("tools.approval.has_blocking_approval", lambda key: False)

    response = await runner._handle_message(
        _event("build and ship the sku killer result bundle UI fix")
    )

    assert response is not None
    assert "Queued as Kanban task `t_abc12345`" in response
    assert runner._running_agents == {}
    assert runner._running_agents_ts == {}


def test_control_plane_task_create_uses_idempotent_kanban_and_subscription(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "gateway"
    runner._active_profile_name = lambda: "app-builder"
    calls = {}

    class Conn:
        def close(self):
            calls["closed"] = True

    def fake_connect():
        calls["connect"] = True
        return Conn()

    def fake_create_task(conn, **kwargs):
        calls["create"] = kwargs
        return "t_deadbeef"

    def fake_add_notify_sub(conn, **kwargs):
        calls["sub"] = kwargs

    monkeypatch.setattr("hermes_cli.kanban_db.connect", fake_connect)
    monkeypatch.setattr("hermes_cli.kanban_db.create_task", fake_create_task)
    monkeypatch.setattr("hermes_cli.kanban_db.add_notify_sub", fake_add_notify_sub)

    task_id, assignee = runner._create_control_plane_kanban_task(
        event=_event("audit the gateway runtime and patch the build workflow"),
        reason="control_plane_work_request",
        user_config={"control_plane": {"default_assignee": "reviewer"}},
    )

    assert task_id == "t_deadbeef"
    assert assignee == "reviewer"
    assert calls["create"]["initial_status"] == "running"
    assert calls["create"]["assignee"] == "reviewer"
    assert calls["create"]["idempotency_key"].startswith("gateway-control-plane:")
    assert calls["sub"]["platform"] == "discord"
    assert calls["sub"]["chat_id"] == "chat-1"
    assert calls["closed"] is True
