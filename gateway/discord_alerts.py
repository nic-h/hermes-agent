"""Opt-in Discord alert routing, formatting, and failure state.

This module keeps owner-specific channel IDs in ``config.yaml`` rather than in
Hermes source. When ``gateway.discord_alerts`` is absent, every caller falls
back to its historical delivery behavior.

Configuration shape::

    gateway:
      discord_alerts:
        channels:
          errors: "<channel-id>"
          status: "<channel-id>"
          kanban: "<channel-id>"
          article_reviews: "<channel-id>"
          social_reviews: "<channel-id>"
        projects:
          article_reviews: ["article-project-keyword"]
          social_reviews: ["social-project-keyword"]
        cooldown_seconds: 600
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


_CHANNEL_KEYS = frozenset(
    {"errors", "status", "kanban", "article_reviews", "social_reviews"}
)
_PROJECT_KEYS = ("article_reviews", "social_reviews")
_DISCORD_CHANNEL_RE = re.compile(r"^\d{15,22}$")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalized_channel(value: Any) -> Optional[str]:
    channel = str(value or "").strip()
    return channel if _DISCORD_CHANNEL_RE.fullmatch(channel) else None


@dataclass(frozen=True)
class DiscordAlertPolicy:
    """Resolved alert channels and project-key routing for one config snapshot."""

    channels: Mapping[str, str]
    projects: Mapping[str, tuple[str, ...]]
    cooldown_seconds: int = 600

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "DiscordAlertPolicy":
        gateway = _mapping(_mapping(config).get("gateway"))
        raw = _mapping(gateway.get("discord_alerts"))
        raw_channels = _mapping(raw.get("channels"))
        channels = {
            key: channel
            for key in _CHANNEL_KEYS
            if (channel := _normalized_channel(raw_channels.get(key))) is not None
        }

        raw_projects = _mapping(raw.get("projects"))
        projects: dict[str, tuple[str, ...]] = {}
        for key in _PROJECT_KEYS:
            values = raw_projects.get(key)
            if not isinstance(values, (list, tuple, set)):
                continue
            normalized = tuple(
                str(value).strip().casefold()
                for value in values
                if str(value).strip()
            )
            if normalized:
                projects[key] = normalized

        try:
            cooldown = int(raw.get("cooldown_seconds", 600))
        except (TypeError, ValueError):
            cooldown = 600
        cooldown = max(1, min(cooldown, 86400))
        return cls(channels=channels, projects=projects, cooldown_seconds=cooldown)

    def project_kind(self, subject: str) -> Optional[str]:
        normalized = str(subject or "").casefold()
        matches: list[tuple[int, str]] = []
        for kind in _PROJECT_KEYS:
            if kind not in self.channels:
                continue
            matches.extend(
                (len(keyword), kind)
                for keyword in self.projects.get(kind, ())
                if keyword in normalized
            )
        # More specific project keys win: "cunicula social" must route to the
        # social review channel even when the article route also has the broad
        # "cunicula" key.
        return max(matches)[1] if matches else None

    def channel_for(self, category: str, *, subject: str = "") -> Optional[str]:
        category = str(category or "").strip().casefold()
        if category == "gateway":
            return self.channels.get("status")
        if category == "kanban":
            return self.channels.get("kanban")
        if category == "failure":
            return self.channels.get("errors")
        if category == "project":
            project_kind = self.project_kind(subject)
            return self.channels.get(project_kind) if project_kind else None
        return None


def load_discord_alert_policy() -> DiscordAlertPolicy:
    """Read the current policy without caching live config changes."""
    try:
        from hermes_cli.config import load_config

        return DiscordAlertPolicy.from_config(load_config())
    except Exception:
        return DiscordAlertPolicy.from_config({})


def discord_alert_metadata(metadata: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Mark an alert as status-only and guarantee it cannot create mentions."""
    merged = dict(metadata or {})
    merged["non_conversational"] = True
    merged["discord_no_mentions"] = True
    return merged


def _cron_subject(job: Mapping[str, Any]) -> str:
    subject = re.sub(
        r"\s+",
        " ",
        str(job.get("name") or "Scheduled task").strip(),
    ) or "Scheduled task"
    return subject if len(subject) <= 120 else subject[:117].rstrip() + "..."


def _failure_class(error: str) -> str:
    text = str(error or "").casefold()
    if "429" in text or "rate limit" in text or "usage limit" in text or "quota" in text:
        return "rate_limit"
    if re.search(r"authenticat|authoriz", text) or re.search(r"\b(?:401|403)\b", text):
        return "authentication"
    if "timeout" in text or "timed out" in text or "readtimeout" in text:
        return "timeout"
    if "http" in text or "request" in text or "connection" in text:
        return "external_service"
    return "failed"


def failure_fingerprint(error: str | None) -> str:
    """Return a stable, content-free fingerprint for cooldown deduplication."""
    return _failure_class(str(error or ""))


def _sanitized_payload(content: str, *, max_chars: int = 12000) -> str:
    """Keep useful review text/media while dropping transport and debug noise."""
    kept: list[str] = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        lowered = stripped.casefold()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if stripped.startswith("MEDIA:"):
            kept.append(stripped)
            continue
        if (
            lowered.startswith("cronjob response:")
            or lowered.startswith("(job_id:")
            or re.fullmatch(r"[-=_]{5,}", stripped)
            or lowered.startswith("traceback")
            or re.match(r'^file\s+".*",\s+line\s+\d+', lowered)
            or re.match(r"^http(?:statuserror)?\s*\d{3}\b", lowered)
            or re.match(r"^\d{4}-\d{2}-\d{2}.*\b(?:debug|info|warning|error)\b", lowered)
        ):
            continue
        kept.append(line)
    payload = "\n".join(kept).strip()
    if len(payload) > max_chars:
        media_lines = [line for line in kept if line.strip().startswith("MEDIA:")]
        media_suffix = "\n".join(media_lines)
        body = "\n".join(
            line for line in kept if not line.strip().startswith("MEDIA:")
        ).strip()
        reserve = len(media_suffix) + (1 if media_suffix else 0)
        body_budget = max(0, max_chars - reserve - 3)
        shortened_body = body[:body_budget].rstrip() + ("..." if body else "")
        payload = "\n".join(
            part for part in (shortened_body, media_suffix) if part
        )
    return payload


def format_cron_alert(
    job: Mapping[str, Any],
    content: str,
    *,
    success: bool,
    project_kind: str,
    recovery: bool = False,
) -> str:
    """Render a project review/failure without IDs, bodies, traces, or log text."""
    subject = _cron_subject(job)
    review_label = {
        "article_reviews": "article",
        "social_reviews": "social",
    }.get(project_kind, "scheduled task")

    if recovery:
        recovery_impact = (
            "The social review flow is available again; publishing remains paused."
            if project_kind == "social_reviews"
            else f"The {review_label} review flow is available again."
        )
        message = (
            f"What happened: {subject} recovered and completed successfully.\n"
            f"Impact: {recovery_impact}\n"
            "Action: Review the new result and choose Approve, Request changes, or Hold."
        )
        review_payload = _sanitized_payload(content)
        return f"{message}\n\nReview:\n{review_payload}" if review_payload else message

    if success:
        impact = (
            "Social publishing remains paused; approval records the decision but does not publish."
            if project_kind == "social_reviews"
            else "Nothing will be published until it is approved."
        )
        message = (
            f"What happened: A {subject} result is ready for review.\n"
            f"Impact: {impact}\n"
            "Action: Approve, Request changes, or Hold."
        )
        review_payload = _sanitized_payload(content)
        return f"{message}\n\nReview:\n{review_payload}" if review_payload else message

    failure = _failure_class(content)
    if failure == "rate_limit":
        happened = f"{subject} could not complete because the provider rate limit was reached."
        action = "Wait for the provider limit to reset or switch the configured provider."
    elif failure == "authentication":
        happened = f"{subject} could not complete because provider authentication failed."
        action = "Check the configured provider credentials, then retry the scheduled task."
    elif failure == "timeout":
        happened = f"{subject} timed out before it produced a review."
        action = "Check provider availability and retry the scheduled task."
    elif failure == "external_service":
        happened = f"{subject} could not complete because an external service request failed."
        action = "Check the upstream service, then retry the scheduled task."
    else:
        happened = f"{subject} did not complete."
        action = "Check the saved cron output and retry after the underlying problem is fixed."
    return (
        f"What happened: {happened}\n"
        f"Impact: The scheduled review was not produced; nothing was published.\n"
        f"Action: {action}"
    )


def format_gateway_alert(state: str, *, detail: str = "") -> str:
    """Render lifecycle state in the same happened/impact/action contract."""
    normalized = str(state or "").strip().casefold()
    if normalized in {"online", "started"}:
        return (
            "What happened: Hermes Gateway is online.\n"
            "Impact: Messaging and scheduled delivery are available.\n"
            "Action: None."
        )
    if normalized in {"restart", "restarting"}:
        return (
            "What happened: Hermes Gateway is restarting.\n"
            "Impact: Messaging and scheduled delivery may pause briefly.\n"
            "Action: Wait for the online confirmation."
        )
    if normalized in {"shutdown", "shutting down"}:
        return (
            "What happened: Hermes Gateway is shutting down.\n"
            "Impact: Messaging and scheduled delivery will be unavailable.\n"
            "Action: Start the gateway again when service should resume."
        )
    if normalized in {"recovered", "connected"}:
        return (
            f"What happened: {detail or 'A gateway platform'} recovered.\n"
            "Impact: Delivery is available again.\n"
            "Action: None."
        )
    return (
        f"What happened: {detail or 'A gateway platform'} is degraded.\n"
        "Impact: Some messages or scheduled deliveries may be delayed.\n"
        "Action: Hermes will retry automatically; investigate if the alert persists."
    )


def format_kanban_alert(message: str) -> str:
    """Keep the useful lifecycle line while making the action explicit."""
    lines = [part.strip() for part in str(message or "").splitlines() if part.strip()]
    line = lines[0] if lines else "Kanban updated"
    if len(line) > 360:
        line = line[:357].rstrip() + "..."
    detail = _sanitized_payload(" ".join(lines[1:]), max_chars=700)
    impact = detail or "Task state changed."
    state = line.split(":", 1)[0].strip().casefold()
    if state in {"blocked", "gave_up"}:
        action = "Resolve the blocker or inspect the task for the required input."
    elif state in {"review", "review_requested"}:
        action = "Review the task and record the decision."
    else:
        action = "Review the Kanban update if input is requested."
    return f"What happened: {line}\nImpact: {impact}\nAction: {action}"


class DiscordAlertDeduper:
    """Thread-safe cooldown and one-shot recovery state, optionally durable."""

    def __init__(
        self,
        *,
        cooldown_seconds: int = 600,
        state_path: Optional[Path] = None,
    ):
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._active: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._state_path = state_path
        self._load()

    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        active = raw.get("active")
        if not isinstance(active, dict):
            return
        for key, value in active.items():
            if not isinstance(value, list) or len(value) != 2:
                continue
            try:
                self._active[str(key)] = (str(value[0]), float(value[1]))
            except (TypeError, ValueError):
                continue

    def _persist_locked(self) -> None:
        if self._state_path is None:
            return
        try:
            from utils import atomic_json_write

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(
                self._state_path,
                {"version": 1, "active": self._active},
                mode=0o600,
            )
        except Exception:
            # Alert delivery must never fail because its dedupe state could not
            # be persisted. The in-process state remains authoritative.
            return

    @staticmethod
    def _timestamp(now: Optional[datetime]) -> float:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    def should_send_failure(
        self,
        key: str,
        fingerprint: str,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        timestamp = self._timestamp(now)
        normalized_key = str(key)
        normalized_fingerprint = str(fingerprint)
        with self._lock:
            previous = self._active.get(normalized_key)
            if (
                previous is not None
                and previous[0] == normalized_fingerprint
                and timestamp - previous[1] < self.cooldown_seconds
            ):
                return False
            self._active[normalized_key] = (normalized_fingerprint, timestamp)
            self._persist_locked()
            return True

    def should_send_recovery(self, key: str) -> bool:
        return self.take_recovery(key) is not None

    def take_recovery(self, key: str) -> Optional[tuple[str, float]]:
        with self._lock:
            recovered = self._active.pop(str(key), None)
            if recovered is not None:
                self._persist_locked()
            return recovered

    def has_active_failure(self, key: str) -> bool:
        with self._lock:
            return str(key) in self._active

    def clear_failure(self, key: str, fingerprint: str) -> None:
        with self._lock:
            current = self._active.get(str(key))
            if current is None or current[0] != str(fingerprint):
                return
            self._active.pop(str(key), None)
            self._persist_locked()

    def restore_failure(
        self,
        key: str,
        state: tuple[str, float],
    ) -> None:
        with self._lock:
            self._active[str(key)] = state
            self._persist_locked()


@dataclass(frozen=True)
class PreparedCronAlert:
    channel_id: str
    content: str
    metadata: Mapping[str, Any]
    transition: Optional[tuple[str, str, str, float]] = None
    cooldown_seconds: int = 600


@dataclass(frozen=True)
class PreparedGatewayAlert:
    channel_id: str
    content: str
    metadata: Mapping[str, Any]
    transition: Optional[tuple[str, str, str, float]] = None
    cooldown_seconds: int = 600


@dataclass(frozen=True)
class PreparedKanbanAlert:
    channel_id: str
    content: str
    metadata: Mapping[str, Any]


_DEDUP_LOCK = threading.Lock()
_DEDUPERS: dict[str, DiscordAlertDeduper] = {}


def _cron_deduper(cooldown_seconds: int) -> DiscordAlertDeduper:
    from hermes_constants import get_hermes_home

    state_path = get_hermes_home() / "state" / "discord_alerts.json"
    key = str(state_path)
    with _DEDUP_LOCK:
        deduper = _DEDUPERS.get(key)
        if deduper is None:
            deduper = DiscordAlertDeduper(
                cooldown_seconds=cooldown_seconds,
                state_path=state_path,
            )
            _DEDUPERS[key] = deduper
        else:
            deduper.cooldown_seconds = max(1, int(cooldown_seconds))
        return deduper


def _gateway_deduper(cooldown_seconds: int) -> DiscordAlertDeduper:
    return _cron_deduper(cooldown_seconds)


def prepare_gateway_alert(
    state: str,
    *,
    detail: str = "",
    failure_key: str = "gateway",
    policy: Optional[DiscordAlertPolicy] = None,
) -> Optional[PreparedGatewayAlert]:
    """Build one status-channel lifecycle alert with failure cooldown/recovery."""
    resolved = policy or load_discord_alert_policy()
    channel_id = resolved.channel_for("gateway")
    if not channel_id:
        return None

    normalized = str(state or "").strip().casefold()
    deduper = _gateway_deduper(resolved.cooldown_seconds)
    if normalized in {"degraded", "failed", "retrying"}:
        fingerprint = failure_fingerprint(detail)
        if not deduper.should_send_failure(failure_key, fingerprint):
            return PreparedGatewayAlert(channel_id, "", {"suppressed": True})
        render_state = "degraded"
        transition = ("failure", failure_key, fingerprint, 0.0)
    elif normalized in {"recovered", "connected"}:
        # Discord cannot report its own outage while disconnected. Its first
        # successful reconnect is therefore itself sufficient recovery proof.
        recovered_state = deduper.take_recovery(failure_key)
        if recovered_state is None and failure_key != "platform:discord":
            return PreparedGatewayAlert(channel_id, "", {"suppressed": True})
        render_state = "recovered"
        transition = (
            ("recovery", failure_key, recovered_state[0], recovered_state[1])
            if recovered_state is not None
            else None
        )
    else:
        render_state = normalized
        transition = None

    return PreparedGatewayAlert(
        channel_id=channel_id,
        content=format_gateway_alert(render_state, detail=detail),
        metadata=discord_alert_metadata(),
        transition=transition,
        cooldown_seconds=resolved.cooldown_seconds,
    )


def prepare_kanban_alert(
    message: str,
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    policy: Optional[DiscordAlertPolicy] = None,
) -> Optional[PreparedKanbanAlert]:
    """Route a Kanban lifecycle update to the one configured Discord channel."""
    resolved = policy or load_discord_alert_policy()
    channel_id = resolved.channel_for("kanban")
    if not channel_id:
        return None
    routed_metadata = discord_alert_metadata(metadata)
    routed_metadata.pop("thread_id", None)
    return PreparedKanbanAlert(
        channel_id=channel_id,
        content=format_kanban_alert(message),
        metadata=routed_metadata,
    )


def prepare_cron_alert(
    job: Mapping[str, Any],
    content: str,
    *,
    success: bool,
    error: str | None = None,
    policy: Optional[DiscordAlertPolicy] = None,
) -> Optional[PreparedCronAlert]:
    """Return a routed project alert, or ``None`` for historical cron delivery."""
    resolved = policy or load_discord_alert_policy()
    subject = _cron_subject(job)
    project_kind = resolved.project_kind(subject)
    key = f"cron:{job.get('id') or subject.casefold()}"
    deduper = _cron_deduper(resolved.cooldown_seconds)

    if success:
        project_channel = resolved.channel_for("project", subject=subject)
        if project_kind and project_channel:
            channel_id = project_channel
        elif deduper.has_active_failure(key):
            channel_id = resolved.channel_for("failure")
            project_kind = project_kind or "scheduled"
        else:
            return None
    else:
        # Failures belong in the narrow errors channel, never in a review or
        # broad/home channel. If no error route is configured, preserve the
        # historical cron destination unchanged.
        channel_id = resolved.channel_for("failure")
        project_kind = project_kind or "scheduled"

    if not channel_id:
        return None

    recovery = False
    transition: Optional[tuple[str, str, str, float]] = None
    if success:
        recovered_state = deduper.take_recovery(key)
        recovery = recovered_state is not None
        if recovered_state is not None:
            transition = ("recovery", key, recovered_state[0], recovered_state[1])
    else:
        fingerprint = failure_fingerprint(error or content)
        if not deduper.should_send_failure(key, fingerprint):
            return PreparedCronAlert(channel_id=channel_id, content="", metadata={"suppressed": True})
        transition = ("failure", key, fingerprint, 0.0)

    return PreparedCronAlert(
        channel_id=channel_id,
        content=format_cron_alert(
            job,
            error or content,
            success=success,
            project_kind=project_kind,
            recovery=recovery,
        ),
        metadata=discord_alert_metadata(),
        transition=transition,
        cooldown_seconds=resolved.cooldown_seconds,
    )


def rollback_prepared_alert(alert: PreparedCronAlert | PreparedGatewayAlert) -> None:
    """Undo a dedupe transition when its outbound delivery was not confirmed."""
    transition = alert.transition
    if transition is None:
        return
    action, key, fingerprint, timestamp = transition
    deduper = _cron_deduper(alert.cooldown_seconds)
    if action == "failure":
        deduper.clear_failure(key, fingerprint)
    elif action == "recovery":
        deduper.restore_failure(key, (fingerprint, timestamp))


__all__ = [
    "DiscordAlertDeduper",
    "DiscordAlertPolicy",
    "PreparedCronAlert",
    "PreparedGatewayAlert",
    "PreparedKanbanAlert",
    "discord_alert_metadata",
    "failure_fingerprint",
    "format_cron_alert",
    "format_gateway_alert",
    "format_kanban_alert",
    "load_discord_alert_policy",
    "prepare_cron_alert",
    "prepare_gateway_alert",
    "prepare_kanban_alert",
    "rollback_prepared_alert",
]
