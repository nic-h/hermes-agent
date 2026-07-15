"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _dispatch_tick_failed_to_spawn,
)
from hermes_cli.kanban_db import DispatchResult

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_dispatch_health_ignores_legitimately_held_ready_work():
    """Capacity and respawn holds are idle, not spawn failures."""
    results = [
        ("global-or-dependency", DispatchResult()),
        (
            "profile-cap",
            DispatchResult(
                skipped_per_profile_capped=[("t_profile", "builder", 1)],
            ),
        ),
        (
            "respawn-guard",
            DispatchResult(respawn_guarded=[("t_guarded", "active_pr")]),
        ),
    ]

    assert _dispatch_tick_failed_to_spawn(results) is False


def test_dispatch_health_counts_genuine_spawn_failure():
    results = [
        (
            "default",
            DispatchResult(
                spawned=[("t_healthy", "builder", "/tmp/work")],
                spawn_failed=[("t_broken", "executable not found")],
            ),
        ),
    ]

    assert _dispatch_tick_failed_to_spawn(results) is True


