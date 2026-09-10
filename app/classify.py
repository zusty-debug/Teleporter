"""Message classification: type detection, link extraction, forum-topic mapping.

Pure functions — no Telegram I/O here, so the logic is unit-testable and reusable
by both the indexer and the migration engine.
"""
import re

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|t\.me/[^\s<>\"')\]]+", re.IGNORECASE)

# ---------------------------------------------------------------- message types

def extract_links(text: str) -> list:
    return URL_RE.findall(text or "")


def message_text(msg) -> str:
    return getattr(msg, "text", None) or getattr(msg, "caption", None) or ""


def message_type(msg) -> str:
    """Classify a pyrogram Message into a canonical content type."""
    if getattr(msg, "service", None):
        return "service"
    if getattr(msg, "poll", None):
        return "poll"
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "animation", None):
        return "animation"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "location", None) or getattr(msg, "venue", None):
        return "location"
    if getattr(msg, "contact", None):
        return "contact"
    text = message_text(msg)
    if extract_links(text):
        return "link"
    if text.strip():
        return "text"
    return "other"


def message_size(msg) -> int:
    for attr in ("photo", "video", "video_note", "animation", "audio", "voice", "document", "sticker"):
        obj = getattr(msg, attr, None)
        if obj is not None and getattr(obj, "file_size", None):
            return int(obj.file_size)
    return 0


def message_sender(msg) -> str:
    u = getattr(msg, "from_user", None)
    if u is None:
        return ""
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    if u.username:
        return f"{name} (@{u.username})".strip() if name else f"@{u.username}"
    return name or str(u.id)


def message_preview(msg, limit: int = 160) -> str:
    text = message_text(msg)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        t = message_type(msg)
        labels = {
            "photo": "📷 Photo", "video": "🎬 Video", "video_note": "⭕ Video message",
            "animation": "🎞 GIF/Animation", "audio": "🎵 Audio", "voice": "🎤 Voice message",
            "document": "📄 Document", "sticker": "😀 Sticker", "poll": "📊 Poll",
            "location": "📍 Location", "contact": "👤 Contact",
        }
        if t == "document" and msg.document and msg.document.file_name:
            return f"📄 {msg.document.file_name}"
        if t == "audio" and msg.audio and msg.audio.title:
            perf = f"{msg.audio.performer} - " if msg.audio.performer else ""
            return f"🎵 {perf}{msg.audio.title}"
        return labels.get(t, "·")
    return text[:limit]


# ---------------------------------------------------------------- copy filters
# Each key is a selectable content filter in the UI.

FILTER_DEFS = [
    ("all",        "All content",            "Copy every supported message type"),
    ("text",       "Text messages",          "Plain text posts (full text, including any links inside)"),
    ("links",      "Links only",             "Extract URLs from messages and copy them as link lists"),
    ("photo",      "Photos",                 ""),
    ("video",      "Videos",                 ""),
    ("video_note", "Video messages (round)", ""),
    ("animation",  "GIFs / animations",      ""),
    ("document",   "Files / documents",      "PDFs, ZIPs, APKs and any other documents"),
    ("audio",      "Music / audio",          ""),
    ("voice",      "Voice messages",         ""),
    ("sticker",    "Stickers",               ""),
    ("poll",       "Polls",                  ""),
    ("location",   "Locations",              ""),
    ("contact",    "Contacts",               ""),
    ("other",      "Other content",          "Anything not covered above"),
]

MEDIA_TYPES = {"photo", "video", "video_note", "animation", "document", "audio", "voice", "sticker"}
TEXTY_TYPES = {"text", "link"}


def should_copy(mtype: str, text: str, filters: set) -> bool:
    """Decide whether a message of the given type passes the user's filter selection."""
    if mtype == "service":
        return False
    if not filters:
        return False
    if "all" in filters:
        return True
    if mtype in TEXTY_TYPES:
        if "text" in filters:
            return True
        if "links" in filters and extract_links(text):
            return True
        return False
    if mtype in filters:
        return True
    if mtype not in MEDIA_TYPES and mtype not in TEXTY_TYPES and "other" in filters:
        return True
    return False


def links_only_mode(filters: set) -> bool:
    """True when the user wants links extracted instead of full text."""
    return "links" in filters and "text" not in filters and "all" not in filters


# ---------------------------------------------------------------- forum topics

DEFAULT_TOPICS = [
    "📁 Documents",
    "🖼️ Photos",
    "🎬 Videos",
    "🎵 Audio & Voice",
    "🔗 Links",
    "💬 Discussion",
    "📦 Other",
]

# Default assignment of each message type to a forum topic title.
# A value of None means "skip this type during topic migration".
DEFAULT_TYPE_TO_TOPIC = {
    "document":   "📁 Documents",
    "photo":      "🖼️ Photos",
    "video":      "🎬 Videos",
    "video_note": "🎬 Videos",
    "animation":  "📦 Other",
    "audio":      "🎵 Audio & Voice",
    "voice":      "🎵 Audio & Voice",
    "sticker":    "📦 Other",
    "poll":       "📦 Other",
    "location":   "📦 Other",
    "contact":    "📦 Other",
    "link":       "🔗 Links",
    "text":       "💬 Discussion",
    "other":      "📦 Other",
    "service":    None,
}

GENERAL_TOPIC_ID = 1  # Telegram's built-in "General" topic in every forum


def default_mapping() -> dict:
    return {
        "topics": list(DEFAULT_TOPICS),
        "types": dict(DEFAULT_TYPE_TO_TOPIC),
        "keywords": [],  # [{"topic": "...", "words": ["..."]}]
    }


def classify_to_topic(mtype: str, text: str, mapping: dict):
    """Return the destination topic title for a message, or None to skip it."""
    if mapping is None:
        mapping = default_mapping()
    types_map = mapping.get("types", DEFAULT_TYPE_TO_TOPIC)
    if mtype not in TEXTY_TYPES:
        # Media & misc types: keyword rules can override the type default.
        text_l = (text or "").lower()
        for rule in mapping.get("keywords", []):
            words = rule.get("words") or []
            for w in words:
                w = (w or "").strip().lower()
                if w and w in text_l:
                    return rule.get("topic")
        return types_map.get(mtype, "📦 Other")
    # Text & link posts are routed purely by keywords so that e.g. a link post
    # about "notes" lands in the Notes topic — not in a generic Links topic.
    text_l = (text or "").lower()
    for rule in mapping.get("keywords", []):
        words = rule.get("words") or []
        for w in words:
            w = (w or "").strip().lower()
            if w and w in text_l:
                return rule.get("topic")
    return types_map.get(mtype, "🔗 Links" if mtype == "link" else "💬 Discussion")
