"""Job engine: indexing + migration with pause/resume/cancel and resume-from-checkpoint.

Every job runs as an asyncio task. State (progress, last processed message id,
copied-message set) is persisted in SQLite so jobs survive restarts and can resume.
"""
import asyncio
import inspect
import time
import uuid
from typing import Optional

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from . import classify, db
from .tg import manager

KINDS = {
    "c2c":   {"label": "Channel → Channel",       "source": "channel", "dest": "channel",  "topics": False, "index": False},
    "c2g":   {"label": "Channel → Group",         "source": "channel", "dest": "group",    "topics": False, "index": False},
    "g2c":   {"label": "Group → Channel",         "source": "group",   "dest": "channel",  "topics": False, "index": False},
    "g2g":   {"label": "Group → Group",           "source": "group",   "dest": "group",    "topics": False, "index": False},
    "c2t":   {"label": "Channel → Forum Topics",  "source": "channel", "dest": "forum",    "topics": True,  "index": True},
    "g2t":   {"label": "Group → Forum Topics",    "source": "group",   "dest": "forum",    "topics": True,  "index": True},
    "index": {"label": "Index Only",              "source": "any",     "dest": None,       "topics": False, "index": True},
}

ACTIVE = {"pending", "indexing", "migrating"}

# One Engine instance per running job (in-memory registry; DB is the source of truth).
engines: dict = {}


def validate_kind_chat(kind: str, role: str, chat: dict) -> Optional[str]:
    """Return an error string if the resolved chat doesn't fit the job kind, else None."""
    spec = KINDS.get(kind)
    if not spec:
        return f"Unknown operation kind: {kind}"
    need = spec["source"] if role == "source" else spec["dest"]
    ctype = chat.get("type")
    if need is None:
        return None
    if need == "channel":
        if ctype != "channel":
            return f"The {'source' if role == 'source' else 'destination'} must be a channel, got '{ctype}'."
    elif need == "group":
        if ctype not in ("group", "supergroup") or (role == "dest" and chat.get("is_forum")):
            if ctype not in ("group", "supergroup"):
                return f"The {'source' if role == 'source' else 'destination'} must be a group, got '{ctype}'."
            if chat.get("is_forum"):
                return "That group has Topics enabled — pick the '→ Forum Topics' operation instead."
    elif need == "forum":
        if ctype != "supergroup" or not chat.get("is_forum"):
            return "The destination must be a supergroup with Topics enabled (a forum)."
    return None


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


async def _log(job_id: str, message: str, level: str = "info") -> None:
    await db.add_log(job_id, message, level)


class Engine:
    def __init__(self, job: dict) -> None:
        self.job_id = job["id"]
        self.kind = job["kind"]
        self.filters = set(job.get("filters") or [])
        self.delay = float(job.get("delay") or 1.0)
        self.stop_flag: Optional[str] = None  # None | 'pause' | 'cancel'
        self.topic_cache: dict = {}

    # ------------------------------------------------------------------ control

    def request_pause(self) -> None:
        self.stop_flag = "pause"

    def request_cancel(self) -> None:
        self.stop_flag = "cancel"

    async def run_index(self) -> None:
        try:
            await db.update_job(self.job_id, status="indexing", phase="index")
            await _log(self.job_id, "Indexing started")
            await self._index_phase()
        except asyncio.CancelledError:  # noqa: BLE001
            pass
        except Exception as e:  # noqa: BLE001
            await db.update_job(self.job_id, status="failed", error=str(e))
            await _log(self.job_id, f"Indexing failed: {e}", "error")
        finally:
            engines.pop(self.job_id, None)

    async def run_migrate(self) -> None:
        try:
            await db.update_job(self.job_id, status="migrating", phase="migrate")
            await _log(self.job_id, "Migration started")
            await self._migrate_phase()
        except asyncio.CancelledError:  # noqa: BLE001
            pass
        except Exception as e:  # noqa: BLE001
            await db.update_job(self.job_id, status="failed", error=str(e))
            await _log(self.job_id, f"Migration failed: {e}", "error")
        finally:
            engines.pop(self.job_id, None)

    async def _check_stop(self) -> bool:
        """Returns True if the loop must stop. Persists pause/cancel state."""
        if self.stop_flag == "pause":
            await db.update_job(self.job_id, status="paused")
            await _log(self.job_id, "Paused by user — progress saved, resume any time.")
            return True
        if self.stop_flag == "cancel":
            await db.update_job(self.job_id, status="canceled")
            await _log(self.job_id, "Canceled by user.", "warn")
            return True
        return False

    # ------------------------------------------------------------------ counting

    async def _history_page(self, client, chat_id, offset_id: int, size: int = 100) -> list:
        """One page of history, retrying through FloodWait errors."""
        for attempt in (1, 2):
            try:
                page = []
                async for m in client.get_chat_history(chat_id, limit=size, offset_id=offset_id):
                    page.append(m)
                return page
            except FloodWait as e:
                secs = min(int(getattr(e, "value", None) or getattr(e, "seconds", 30)), 600)
                await _log(self.job_id, f"Flood wait {secs}s from Telegram — sleeping…", "warn")
                await asyncio.sleep(secs + 1)
                if attempt == 2:
                    raise
        return []

    async def _pages(self, client, chat_id, offset_id: int, page_size: int = 100):
        """Yield pages of messages older than offset_id, newest-first per page."""
        while True:
            page = await self._history_page(client, chat_id, offset_id, page_size)
            if not page:
                return
            yield page
            offset_id = page[-1].id  # oldest in this page

    # ------------------------------------------------------------------ index phase

    async def _index_phase(self) -> None:
        job = await db.get_job(self.job_id)
        client = await manager.ensure()
        source = job["source"]["id"]

        if not job.get("total"):
            total = await manager.get_message_count(source)
            job["total"] = total
            await db.update_job(self.job_id, total=total)
            if total:
                await _log(self.job_id, f"Source has {total} messages — scanning now (live progress)")
            else:
                await _log(self.job_id, "Telegram didn't report a total for this chat — running count only")

        batch = []
        processed = job.get("processed") or 0
        last_id = job.get("last_msg_id") or 0
        last_logged = processed

        async for page in self._pages(client, source, last_id, page_size=500):
            for msg in page:
                mtype = classify.message_type(msg)
                text = classify.message_text(msg)
                batch.append({
                    "msg_id": msg.id,
                    "ts": msg.date.timestamp() if msg.date else 0,
                    "type": mtype,
                    "sender": classify.message_sender(msg),
                    "size": classify.message_size(msg),
                    "urls": classify.extract_links(text),
                    "preview": classify.message_preview(msg),
                })
                processed += 1
            await db.insert_index_rows(self.job_id, batch)
            batch.clear()
            last_id = page[-1].id
            await db.update_job(self.job_id, processed=processed, last_msg_id=last_id)
            if processed - last_logged >= 2000:
                tot = job.get("total") or 0
                await _log(self.job_id, f"Indexed {processed:,}{' / ' + f'{tot:,}' if tot else ''} messages")
                last_logged = processed
            if await self._check_stop():
                return

        await db.update_job(self.job_id, processed=processed)
        await db.insert_index_rows(self.job_id, batch)

        spec = KINDS[self.kind]
        if self.kind == "index":
            await db.update_job(self.job_id, status="done", phase="index")
            await _log(self.job_id, "Indexing finished — report is ready.")
        elif spec["topics"]:
            await db.update_job(self.job_id, status="awaiting_mapping", phase="index",
                                mapping=classify.default_mapping())
            await _log(self.job_id, "Indexing finished. Review the topic plan, then start migration.")
        else:
            # plain copy kinds don't index; this path is unused but kept safe
            await db.update_job(self.job_id, status="done", phase="index")

    # ------------------------------------------------------------------ migrate phase

    async def _migrate_phase(self) -> None:
        job = await db.get_job(self.job_id)
        client = await manager.ensure()
        source = job["source"]["id"]
        dest = job["dest"]["id"]
        is_topics = KINDS[self.kind]["topics"]
        mapping = job.get("mapping") if is_topics else None
        already = await db.copied_set(self.job_id)
        links_only = classify.links_only_mode(self.filters)

        if not job.get("total"):
            total = await manager.get_message_count(source)
            await db.update_job(self.job_id, total=total)

        counters = {"copied": job.get("copied") or 0, "skipped": job.get("skipped") or 0,
                    "failed": job.get("failed") or 0, "processed": job.get("processed") or 0}
        last_id = job.get("last_msg_id") or 0
        copied_batch = []
        since_flush = time.time()

        await _log(self.job_id, f"Copying from {source} to {dest} "
                                f"(delay {self.delay}s between messages, resume from #{last_id or 'start'})")

        async for page in self._pages(client, source, last_id):
            for msg in reversed(page):  # chronological order
                if msg.id <= last_id:
                    continue
                if await self._check_stop():
                    await db.mark_copied(self.job_id, copied_batch)
                    return
                counters["processed"] += 1
                mtype = classify.message_type(msg)
                text = classify.message_text(msg)

                if msg.id in already or not classify.should_copy(mtype, text, self.filters):
                    counters["skipped"] += 1
                else:
                    thread_id = None
                    if is_topics:
                        topic = classify.classify_to_topic(mtype, text, mapping)
                        if topic is None:
                            counters["skipped"] += 1
                            last_id = msg.id
                            continue
                        thread_id = await manager.ensure_topic(dest, topic, self.topic_cache)
                    ok, err = await self._copy_one(client, msg, dest, thread_id, links_only)
                    if ok:
                        counters["copied"] += 1
                        already.add(msg.id)
                        copied_batch.append(msg.id)
                    else:
                        counters["failed"] += 1
                        await _log(self.job_id, f"Failed to copy message #{msg.id}: {err}", "error")
                last_id = msg.id

                if time.time() - since_flush > 2 or len(copied_batch) >= 50:
                    await db.mark_copied(self.job_id, copied_batch)
                    copied_batch.clear()
                    await db.update_job(self.job_id, last_msg_id=last_id, **counters)
                    since_flush = time.time()

            await db.mark_copied(self.job_id, copied_batch)
            copied_batch.clear()
            await db.update_job(self.job_id, last_msg_id=last_id, **counters)
            if counters["copied"] and counters["processed"] % 500 < 100:
                await _log(self.job_id, f"Progress: {counters['copied']} copied / "
                                        f"{counters['skipped']} skipped / {counters['failed']} failed")

        await db.mark_copied(self.job_id, copied_batch)
        await db.update_job(self.job_id, status="done", last_msg_id=last_id, **counters)
        await _log(self.job_id, f"Migration finished: {counters['copied']} copied, "
                                f"{counters['skipped']} skipped, {counters['failed']} failed.")

    # ------------------------------------------------------------------ copy one message

    async def _copy_one(self, client, msg, dest, thread_id, links_only: bool):
        """Re-upload/duplicate a single message into dest. Returns (ok, error_message).

        This is a true copy: content is re-sent from our session, so no
        'Forwarded from…' tag appears on the destination.
        """
        try:
            for attempt in (1, 2):
                try:
                    await self._send_copy(client, msg, dest, thread_id, links_only)
                    return True, None
                except FloodWait as e:
                    secs = getattr(e, "value", None) or getattr(e, "seconds", 30)
                    secs = min(int(secs), 600)
                    await _log(self.job_id, f"Flood wait {secs}s from Telegram — sleeping…", "warn")
                    await asyncio.sleep(secs + 1)
                    if attempt == 2:
                        return False, f"flood wait after retry ({secs}s)"
                except Exception as e:  # noqa: BLE001
                    return False, f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        return False, "unknown"

    async def _send_copy(self, client, msg, dest, thread_id, links_only: bool) -> None:
        mtype = classify.message_type(msg)
        caption = msg.caption or ""
        cap_ent = msg.caption_entities or []
        kw = {"parse_mode": ParseMode.DISABLED}
        if thread_id is not None and thread_id != 1:
            kw["message_thread_id"] = thread_id

        async def download():
            buf = await client.download_media(msg, in_memory=True)
            if buf is None:
                raise RuntimeError("download returned nothing")
            buf.seek(0)
            return buf

        if mtype == "photo":
            buf = await download()
            await client.send_photo(dest, buf, caption=caption or None,
                                    caption_entities=cap_ent or None, **kw)
        elif mtype == "video":
            v = msg.video
            buf = await download()
            await client.send_video(dest, buf, duration=v.duration or 0, width=v.width or 0,
                                    height=v.height or 0, file_name=v.file_name or None,
                                    supports_streaming=bool(v.supports_streaming),
                                    caption=caption or None, caption_entities=cap_ent or None, **kw)
        elif mtype == "video_note":
            v = msg.video_note
            buf = await download()
            await client.send_video_note(dest, buf, duration=v.duration or 0,
                                         length=v.length or 0, **kw)
        elif mtype == "animation":
            a = msg.animation
            buf = await download()
            await client.send_animation(dest, buf, duration=a.duration or 0, width=a.width or 0,
                                        height=a.height or 0, file_name=a.file_name or None,
                                        caption=caption or None, caption_entities=cap_ent or None, **kw)
        elif mtype == "document":
            d = msg.document
            buf = await download()
            await client.send_document(dest, buf, file_name=d.file_name or None,
                                       caption=caption or None, caption_entities=cap_ent or None, **kw)
        elif mtype == "audio":
            a = msg.audio
            buf = await download()
            await client.send_audio(dest, buf, duration=a.duration or 0, performer=a.performer or None,
                                    title=a.title or None, file_name=a.file_name or None,
                                    caption=caption or None, caption_entities=cap_ent or None, **kw)
        elif mtype == "voice":
            v = msg.voice
            buf = await download()
            await client.send_voice(dest, buf, duration=v.duration or 0,
                                    caption=caption or None, caption_entities=cap_ent or None, **kw)
        elif mtype == "sticker":
            buf = await download()
            await _call_with_thread(client.send_sticker, dest, buf, thread_id)
        elif mtype == "poll":
            p = msg.poll
            options = [o.text for o in (p.options or [])]
            await _call_with_thread(client.send_poll, dest, p.question, options, thread_id)
        elif mtype == "location":
            loc = msg.location or getattr(msg, "venue", None) and msg.venue.location
            await _call_with_thread(client.send_location, dest, loc.latitude, loc.longitude, thread_id)
        elif mtype == "contact":
            c = msg.contact
            await _call_with_thread(client.send_contact, dest, c.phone_number,
                                    first_name=c.first_name or "", last_name=c.last_name or None,
                                    thread_id=thread_id)
        elif mtype in ("text", "link"):
            text = msg.text or ""
            if links_only:
                urls = classify.extract_links(text) or classify.extract_links(caption)
                if not urls:
                    raise RuntimeError("no links found")
                text = "\n".join(urls)
                ent = None
            else:
                ent = msg.entities or None
            await client.send_message(dest, text, entities=ent, **kw)
        else:
            # Unsupported exotic type → keep a trace as text instead of silently dropping.
            await client.send_message(dest, f"[{mtype} message copied by Teleporter]", **kw)

        if self.delay > 0:
            await asyncio.sleep(self.delay)


async def _call_with_thread(fn, *args, thread_id=None, **kwargs):
    """Call a send method, adding message_thread_id only if it supports it."""
    if thread_id is not None and thread_id != 1:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        if "message_thread_id" in params:
            kwargs["message_thread_id"] = thread_id
    await fn(*args, **kwargs)


# ------------------------------------------------------------------ public helpers

def start_job(job: dict) -> Engine:
    """Create and launch the right engine phase for a job."""
    eng = Engine(job)
    engines[job["id"]] = eng
    spec = KINDS[job["kind"]]
    if spec["index"] and (job.get("phase") or "index") == "index":
        asyncio.create_task(eng.run_index())
    else:
        asyncio.create_task(eng.run_migrate())
    return eng


async def resume_job(job: dict) -> Engine:
    eng = Engine(job)
    engines[job["id"]] = eng
    if job.get("phase") == "index" and job.get("status") == "paused" and KINDS[job["kind"]]["index"]:
        asyncio.create_task(eng.run_index())
    else:
        asyncio.create_task(eng.run_migrate())
    return eng
