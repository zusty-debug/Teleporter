"""Telegram client management: authentication, chats, forum topics.

Uses Pyrofork (maintained Pyrogram fork) with an in-memory user session.
The session string is what authenticates everything — a full user account.
"""
import asyncio
from typing import Optional

from pyrogram import Client
from pyrogram.errors import (
    ApiIdInvalid,
    AccessTokenInvalid,
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    PhoneNumberUnoccupied,
    SessionPasswordNeeded,
    UsernameInvalid,
    UsernameNotOccupied,
)

from . import db

CLIENT_NAME = "teleporter"


class AuthError(Exception):
    """Friendly authentication error surfaced to the UI."""


class TwoFactorRequired(AuthError):
    """The code was accepted but the account needs its 2FA password."""


def _friendly(e: Exception, context: str) -> str:
    if isinstance(e, ApiIdInvalid):
        return "Invalid API ID / API Hash. Double-check the values from my.telegram.org."
    if isinstance(e, PhoneNumberInvalid):
        return "Invalid phone number. Use the full international format, e.g. +919812345678."
    if isinstance(e, PhoneCodeInvalid):
        return "The login code is wrong. Check the code Telegram sent you."
    if isinstance(e, PhoneCodeExpired):
        return "The login code expired. Start the login flow again."
    if isinstance(e, PasswordHashInvalid):
        return "Wrong Two-Step Verification password."
    if isinstance(e, PhoneNumberUnoccupied):
        return "This phone number has no Telegram account."
    if isinstance(e, FloodWait):
        secs = getattr(e, "value", None) or getattr(e, "seconds", 60)
        return f"Telegram flood limit reached. Try again in {secs} seconds."
    return f"{context}: {type(e).__name__}: {e}"


class TelegramManager:
    """Holds the single authenticated user client (single-user app)."""

    def __init__(self) -> None:
        self.client: Optional[Client] = None
        self.profile: Optional[dict] = None
        # pending phone-number login state
        self._login_client: Optional[Client] = None
        self._login_phone: Optional[str] = None
        self._login_code_hash: Optional[str] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- sessions

    async def connect_with_session(self, api_id: int, api_hash: str, session_string: str) -> dict:
        """Validate a pasted session string and make it the active client."""
        client = Client(
            CLIENT_NAME,
            api_id=int(api_id),
            api_hash=str(api_hash),
            session_string=session_string.strip(),
            in_memory=True,
        )
        try:
            await client.connect()
            me = await client.get_me()
        except Exception as e:  # noqa: BLE001
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            msg = str(e).lower()
            if "unpack" in msg or "buffer" in msg or "string decode" in msg or "invalid padding" in msg:
                raise AuthError(
                    "That session string looks invalid or corrupted. It should be a long "
                    "string produced by Pyrogram — re-copy it or generate a new one."
                ) from e
            raise AuthError(_friendly(e, "Could not authenticate with this session string")) from e

        await self._swap_client(client)
        self.profile = _profile_dict(me)
        await self._persist_credentials(api_id, api_hash, session_string.strip())
        return self.profile

    async def _swap_client(self, client: Client) -> None:
        old = self.client
        self.client = client
        if old is not None and old is not client:
            try:
                await old.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _persist_credentials(self, api_id, api_hash, session_string) -> None:
        import json
        await db.kv_set("tg_session", json.dumps({
            "api_id": int(api_id), "api_hash": str(api_hash), "session_string": session_string,
        }))

    async def restore_saved_session(self) -> Optional[dict]:
        """On app boot, reconnect with the saved session if there is one."""
        import json
        raw = await db.kv_get("tg_session")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return await self.connect_with_session(data["api_id"], data["api_hash"], data["session_string"])
        except Exception:  # noqa: BLE001
            return None

    async def disconnect(self) -> None:
        await self._swap_client(None)
        self.profile = None
        await db.kv_delete("tg_session")

    async def ensure(self) -> Client:
        if self.client is None:
            raise AuthError("Not authenticated. Connect a Telegram account first.")
        if not await self._is_connected():
            try:
                await self.client.connect()
            except Exception as e:  # noqa: BLE001
                raise AuthError(f"Reconnect failed: {e}") from e
        return self.client

    async def _is_connected(self) -> bool:
        try:
            return bool(self.client and self.client.is_connected)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------- phone login

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> None:
        """Step 1 of session-string generation: send the login code."""
        if self._login_client is not None:
            try:
                await self._login_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._login_client = None
        phone = phone.strip().replace(" ", "")
        client = Client(f"{CLIENT_NAME}_login", api_id=int(api_id), api_hash=str(api_hash), in_memory=True)
        try:
            await client.connect()
            sent = await client.send_code(phone)
        except Exception as e:  # noqa: BLE001
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise AuthError(_friendly(e, "Could not send login code")) from e
        self._login_client = client
        self._login_phone = phone
        self._login_code_hash = sent.phone_code_hash

    async def verify_code(self, code: str, password: Optional[str] = None):
        """Step 2: verify code (+ optional 2FA password). Returns (session_string, profile)."""
        if self._login_client is None:
            raise AuthError("No login in progress. Send the code first.")
        try:
            try:
                await self._login_client.sign_in(self._login_phone, self._login_code_hash, code.strip())
            except SessionPasswordNeeded:
                if not password:
                    raise TwoFactorRequired("Two-Step Verification is enabled. Enter your password.")
                await self._login_client.check_password(password)
            me = await self._login_client.get_me()
            session_string = await self._login_client.export_session_string()
        except TwoFactorRequired:
            raise
        except Exception as e:  # noqa: BLE001
            raise AuthError(_friendly(e, "Login failed")) from e

        # Promote the login client to be the main client.
        client = self._login_client
        self._login_client, self._login_phone, self._login_code_hash = None, None, None
        await self._swap_client(client)
        self.profile = _profile_dict(me)
        api_id, api_hash = client.api_id, client.api_hash
        await self._persist_credentials(api_id, api_hash, session_string)
        return session_string, self.profile

    async def cancel_login(self) -> None:
        if self._login_client is not None:
            try:
                await self._login_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._login_client = self._login_phone = self._login_code_hash = None

    # ------------------------------------------------------------- chats

    async def list_dialogs(self, query: str = "") -> list:
        """Groups/channels/supergroups visible to the user, for pickers."""
        client = await self.ensure()
        q = (query or "").strip().lower()
        out = []
        try:
            async for dialog in client.get_dialogs(limit=500):
                chat = dialog.chat
                if chat.type not in ("group", "supergroup", "channel"):
                    continue
                info = _chat_dict(chat)
                if q:
                    hay = f"{info['title']} {info.get('username') or ''}".lower()
                    if q not in hay:
                        continue
                out.append(info)
                if len(out) >= 100:
                    break
        except Exception as e:  # noqa: BLE001
            raise AuthError(_friendly(e, "Could not list your chats")) from e
        return out

    async def resolve_chat(self, target) -> dict:
        """Resolve @username / invite link / numeric id into chat info."""
        client = await self.ensure()
        target = str(target).strip()
        if not target:
            raise AuthError("Provide a chat link, @username or numeric ID.")
        if target.lstrip("-").isdigit():
            target = int(target)
        try:
            chat = await client.get_chat(target)
        except (UsernameInvalid, UsernameNotOccupied):
            raise AuthError(f"Chat not found: {target}. Check the @username or link.")
        except AccessTokenInvalid:
            raise AuthError("Invalid invite link.")
        except Exception as e:  # noqa: BLE001
            raise AuthError(_friendly(e, f"Could not open chat {target}")) from e
        return _chat_dict(chat)

    # ------------------------------------------------------------- forum topics

    async def get_topics(self, chat_id) -> list:
        client = await self.ensure()
        try:
            topics = await client.get_forum_topics(chat_id, limit=200)
        except Exception as e:  # noqa: BLE001
            raise AuthError(_friendly(e, "Could not read forum topics")) from e
        out = [{"id": t.id, "title": t.title} for t in topics]
        if not any(t["id"] == 1 for t in out):
            out.insert(0, {"id": 1, "title": "General"})
        return out

    async def ensure_topic(self, chat_id, title: str, cache: dict) -> Optional[int]:
        """Get-or-create a forum topic by title. Returns the topic (thread) id."""
        if title in cache:
            return cache[title]
        if title.strip().lower() == "general":
            cache[title] = 1
            return 1
        client = await self.ensure()
        # look among existing topics first
        try:
            topics = await client.get_forum_topics(chat_id, limit=200)
            for t in topics:
                cache[t.title] = t.id
                if t.title == title:
                    return t.id
        except Exception:  # noqa: BLE001
            pass
        try:
            created = await client.create_forum_topic(chat_id, title)
            cache[title] = created.id
            return created.id
        except Exception as e:  # noqa: BLE001
            raise AuthError(_friendly(e, f"Could not create topic '{title}'")) from e


def _profile_dict(me) -> dict:
    name = " ".join(p for p in [me.first_name or "", me.last_name or ""] if p).strip()
    return {
        "id": me.id,
        "name": name or (me.username or str(me.id)),
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "username": me.username or "",
        "phone_number": getattr(me, "phone_number", "") or "",
        "is_premium": bool(getattr(me, "is_premium", False)),
        "is_bot": bool(me.is_bot),
    }


def _chat_dict(chat) -> dict:
    return {
        "id": chat.id,
        "title": getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id),
        "type": chat.type,  # group | supergroup | channel
        "username": getattr(chat, "username", None) or "",
        "is_forum": bool(getattr(chat, "is_forum", False)),
        "members_count": getattr(chat, "members_count", None),
        "has_protected_content": bool(getattr(chat, "has_protected_content", False)),
    }


# Singleton instance shared by the whole app.
manager = TelegramManager()
