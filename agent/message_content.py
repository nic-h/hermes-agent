from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_NON_TEXT_PART_TYPES = {"image", "image_url", "input_image", "audio", "input_audio"}
_TEXT_KEYS = ("text", "content", "input_text", "output_text", "summary_text")
_VISIBLE_ASSISTANT_PART_TYPES = {"text", "output_text"}


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _text_from_part(part: Any) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part

    part_type = str(_field(part, "type") or "").strip().lower()
    if part_type in _NON_TEXT_PART_TYPES:
        return ""

    for key in _TEXT_KEYS:
        text = _field(part, key)
        if isinstance(text, str):
            return text
    return ""


def _visible_assistant_text_from_part(part: Any) -> str:
    """Extract only user-visible text from one assistant content part."""
    if isinstance(part, str):
        return part
    if part is None:
        return ""

    part_type = _field(part, "type")
    if part_type is not None:
        if not isinstance(part_type, str):
            return ""
        if part_type.strip().lower() not in _VISIBLE_ASSISTANT_PART_TYPES:
            return ""

    # Untyped ``text`` / ``content`` objects are accepted for compatibility
    # with OpenAI-compatible servers. Explicitly typed reasoning, tool, media,
    # and unknown blocks were rejected above, so their payloads stay hidden.
    for key in ("text", "content"):
        text = _field(part, key)
        if isinstance(text, str):
            return text
    return ""


def flatten_message_text(content: Any, *, sep: str = "\n") -> str:
    """Return the visible text from common chat/Responses message content shapes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = [_text_from_part(part) for part in content]
        return sep.join(chunk for chunk in chunks if chunk)

    text = _text_from_part(content)
    if text:
        return text
    try:
        return str(content)
    except Exception:
        return ""


def flatten_assistant_visible_text(content: Any, *, sep: str = "\n") -> str:
    """Return visible assistant text without stringifying unknown payloads."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = [_visible_assistant_text_from_part(part) for part in content]
        return sep.join(chunk for chunk in chunks if chunk)
    return _visible_assistant_text_from_part(content)
