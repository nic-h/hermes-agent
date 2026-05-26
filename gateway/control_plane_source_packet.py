"""Durable source-packet helpers for gateway control-plane Kanban offload.

Large Discord/Codex requests should not be copied wholesale into Kanban task
bodies or active provider prompts. This module preserves the request in a
0600 JSON packet after redaction, optionally writes a wiki pointer, and builds a
bounded task body that tells the worker where to recover missing detail.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

SOURCE_PACKET_DIRNAME = "control-plane-source-packets"
DEFAULT_ACTIVE_EXCERPT_CHARS = 6_000


def _platform_value(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


def _redact_text(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text or "", force=True)
    except Exception:
        return "[redaction unavailable; excerpt omitted]"


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def source_packet_dirs(*, hermes_home: Optional[Path] = None, wiki_home: Optional[Path] = None) -> tuple[Path, Path]:
    raw_base = (Path(hermes_home) if hermes_home is not None else get_hermes_home()).expanduser().resolve()
    wiki_base = (Path(wiki_home) if wiki_home is not None else Path.home() / "wiki").expanduser().resolve()
    return raw_base / "state" / SOURCE_PACKET_DIRNAME, wiki_base / "outputs" / SOURCE_PACKET_DIRNAME


def write_control_plane_source_packet(
    event: Any,
    *,
    reason: str,
    active_excerpt_chars: int = DEFAULT_ACTIVE_EXCERPT_CHARS,
    hermes_home: Optional[Path] = None,
    wiki_home: Optional[Path] = None,
) -> dict[str, Any]:
    """Persist redacted full request/context and return bounded-task metadata."""
    source = getattr(event, "source", None)
    platform = _platform_value(getattr(source, "platform", None))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    text = getattr(event, "text", None) or ""
    media_urls = list(getattr(event, "media_urls", None) or [])
    media_types = list(getattr(event, "media_types", None) or [])
    redacted_text = _redact_text(text).replace("```", "''' ")
    redacted_reply_to_text = _redact_text(getattr(event, "reply_to_text", None) or "") if getattr(event, "reply_to_text", None) else None
    redacted_channel_context = _redact_text(getattr(event, "channel_context", None) or "") if getattr(event, "channel_context", None) else None
    redacted_channel_prompt = _redact_text(getattr(event, "channel_prompt", None) or "") if getattr(event, "channel_prompt", None) else None
    digest_basis = "\n".join(
        [
            platform,
            str(getattr(source, "chat_id", "") or ""),
            str(getattr(source, "thread_id", "") or ""),
            str(getattr(source, "user_id", "") or ""),
            str(getattr(event, "message_id", "") or ""),
            text,
        ]
    )
    digest = hashlib.sha256(digest_basis.encode("utf-8", "ignore")).hexdigest()[:16]
    raw_dir, wiki_dir = source_packet_dirs(hermes_home=hermes_home, wiki_home=wiki_home)
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(raw_dir, 0o700)
    except OSError:
        pass

    raw_path = raw_dir / f"{timestamp}-{platform or 'gateway'}-{digest}.json"
    packet = {
        "kind": "gateway_control_plane_source_packet",
        "created_at": datetime.now().isoformat(),
        "reason": reason,
        "origin": {
            "platform": platform,
            "chat_id": getattr(source, "chat_id", None),
            "thread_id": getattr(source, "thread_id", None),
            "user_id": getattr(source, "user_id", None),
            "user_name": getattr(source, "user_name", None),
            "chat_type": getattr(source, "chat_type", None),
            "chat_name": getattr(source, "chat_name", None),
            "message_id": getattr(event, "message_id", None),
            "platform_update_id": getattr(event, "platform_update_id", None),
            "timestamp": getattr(event, "timestamp", None),
        },
        "request": {
            "text": redacted_text,
            "reply_to_message_id": getattr(event, "reply_to_message_id", None),
            "reply_to_text": redacted_reply_to_text,
            "channel_context": redacted_channel_context,
            "channel_prompt": redacted_channel_prompt,
            "auto_skill": getattr(event, "auto_skill", None),
        },
        "attachments": [
            {"path": path, "media_type": media_types[i] if i < len(media_types) else ""}
            for i, path in enumerate(media_urls)
        ],
        "recovery_policy": {
            "active_prompt_payload": "bounded excerpt and pointers only",
            "full_context": "preserved in this raw source packet after secret/PII redaction; retrieve focused slices with file/search tools",
            "do_not": "paste the whole raw packet into a provider prompt unless the user explicitly asks",
        },
    }
    _atomic_write_json(raw_path, packet, mode=0o600)

    active_excerpt_chars = max(1, int(active_excerpt_chars or DEFAULT_ACTIVE_EXCERPT_CHARS))
    excerpt = redacted_text[:active_excerpt_chars]
    omitted_chars = max(0, len(text) - active_excerpt_chars)
    wiki_path: Optional[Path] = None
    wiki_error: Optional[str] = None
    try:
        wiki_path = wiki_dir / f"{timestamp}-{platform or 'gateway'}-{digest}.md"
        markdown = (
            f"# Gateway control-plane source packet — {timestamp[:8]}\n\n"
            "This note preserves the full chat control-surface request durably while keeping active provider/worker prompts bounded.\n\n"
            f"- Raw local packet: `{raw_path}`\n"
            f"- Platform/chat/thread/message: `{platform}` / `{getattr(source, 'chat_id', None)}` / `{getattr(source, 'thread_id', None)}` / `{getattr(event, 'message_id', None)}`\n"
            f"- Offload reason: `{reason}`\n"
            "- Retrieval policy: read/search this pointer layer first; retrieve focused slices from the raw packet only when needed.\n\n"
            "## Redacted active excerpt\n\n"
            f"```text\n{excerpt}\n```\n"
        )
        _atomic_write_text(wiki_path, markdown, mode=0o644)
    except Exception as exc:
        wiki_error = f"{type(exc).__name__}: {exc}"
        wiki_path = None

    return {
        "raw_path": str(raw_path),
        "wiki_path": str(wiki_path) if wiki_path else None,
        "wiki_error": wiki_error,
        "digest": digest,
        "excerpt": excerpt,
        "omitted_chars": omitted_chars,
        "text_chars": len(text),
        "attachment_count": len(media_urls),
    }


def build_control_plane_task_body(event: Any, *, reason: str, packet: dict[str, Any]) -> str:
    source = getattr(event, "source", None)
    platform = _platform_value(getattr(source, "platform", None))
    chat_id = str(getattr(source, "chat_id", "") or "")
    thread_id = str(getattr(source, "thread_id", "") or "")
    user_id = str(getattr(source, "user_id", "") or "")
    message_id = str(getattr(event, "message_id", "") or "")
    excerpt = str(packet.get("excerpt") or "").strip() or "(empty)"
    omitted = int(packet.get("omitted_chars") or 0)
    omitted_line = f"\n[... {omitted} chars omitted from active task packet; redacted full request is in the raw source packet.]" if omitted else ""
    wiki_line = f"- Wiki source packet: `{packet.get('wiki_path')}`\n" if packet.get("wiki_path") else ""
    wiki_error_line = f"- Wiki source packet: unavailable (`{packet.get('wiki_error')}`); raw packet is still preserved.\n" if packet.get("wiki_error") else ""
    return (
        "Created automatically from a gateway control-plane message so the chat surface stays responsive while preserving the full source context durably. "
        "Execute the work in a Kanban worker; do not turn this back into an inline gateway conversation.\n\n"
        "Context policy:\n"
        "- Discord/Codex remains the rich control surface; information is preserved, not discarded.\n"
        "- This Kanban body is a bounded active task packet, not the full raw transcript.\n"
        "- Retrieve missing detail by reading/searching the durable source packet paths below; do not paste raw bundles wholesale into provider prompts.\n\n"
        f"Offload reason: {reason}\n"
        f"Origin: platform={platform or 'unknown'} chat={chat_id or 'unknown'} thread={thread_id or 'none'} user={user_id or 'unknown'} message={message_id or 'none'}\n\n"
        "Durable context pointers:\n"
        f"- Raw source packet (0600, redacted full request/context): `{packet.get('raw_path')}`\n"
        f"{wiki_line}{wiki_error_line}"
        f"- Source packet digest: `{packet.get('digest')}`\n"
        f"- Attachment pointers in packet: `{packet.get('attachment_count', 0)}`\n\n"
        "Current request excerpt (redacted, bounded):\n"
        f"{excerpt}{omitted_line}\n\n"
        "Acceptance:\n"
        "- Work runs in the assigned worker process, not the gateway request path.\n"
        "- Preserve any needed context by linking/reading durable packets, wiki notes, Honcho/search, Kanban comments, or artifacts.\n"
        "- Leave durable progress and a structured Kanban completion.\n"
        "- Notify/block through Kanban if human input is genuinely required.\n"
    )
