"""Job engine: indexing + migration with pause/resume/cancel and resume-from-checkpoint.

Every job runs as an asyncio task. State (progress, last processed message id,
copied-message set) is persisted in SQLite so jobs survive restarts and can resume.
"""
import asyncio
import inspect
import time
import traceback
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.raw import types as raw_types
from pyrogram.raw.functions.messages import GetHistory

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
    """Return an error string if the resolved chat doesn't fit the job kind, else None.

    Note: pyrofork reports topic-enabled supergroups as type "forum".
    """
    spec = KINDS.get(kind)
    if not spec:
        return f"Unknown operation kind: {kind}"
    need = spec["source"] if role == "source" else spec["dest"]
    ctype = chat.get("type")
    is_forum = bool(chat.get("is_forum")) or ctype == "forum"
    group_types = {"group", "supergroup", "forum"}
    if need is None:
        return None
    if need == "channel":
        if ctype != "channel":
            return f"The {'source' if role == 'source' else 'destination'} must be a channel, got '{ctype}'."
    elif need == "group":
        if ctype not in group_types:
            return f"The {'source' if role == 'source' else 'destination'} must be a group, got '{ctype}'."
        if role == "dest" and is_forum:
            return "That group has Topics enabled — pick the '→ Forum Topics' operation instead."
    elif need == "forum":
        if not (ctype == "forum" or (ctype == "supergroup" and is_forum)):
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

    async def _safety_net(self) -> None:
        """If the engine exits while the DB still says active, mark the job failed
        with a helpful message — a job must never sit as a silent zombie."""
        try:
            job = await db.get_job(self.job_id)
            if job and job["status"] in ACTIVE:
                await db.update_job(
                    self.job_id, status="failed",
                    error="Engine stopped unexpectedly (server restart or crash). "
                          "Press Resume — it continues from the saved checkpoint.")
                await _log(self.job_id,
                           "Engine stopped unexpectedly — marked as Failed. "
                           "Use Resume to continue from the last checkpoint.", "error")
        except Exception:  # noqa: BLE001
            pass

    async def run_index(self) -> None:
        try:
            await db.update_job(self.job_id, status="indexing", phase="index")
            await _log(self.job_id, "Indexing started")
            await self._index_phase()
        except asyncio.CancelledError:  # noqa: BLE001
            pass
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()[-700:].replace("\n", " | ")
            await db.update_job(self.job_id, status="failed", error=str(e))
            await _log(self.job_id, f"Indexing failed: {e} — {tb}", "error")
        finally:
            await self._safety_net()
            engines.pop(self.job_id, None)

    async def run_migrate(self) -> None:
        try:
            await db.update_job(self.job_id, status="migrating", phase="migrate")
            await _log(self.job_id, "Migration started")
            await self._migrate_phase()
        except asyncio.CancelledError:  # noqa: BLE001
            pass
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()[-700:].replace("\n", " | ")
            await db.update_job(self.job_id, status="failed", error=str(e))
            await _log(self.job_id, f"Migration failed: {e} — {tb}", "error")
        finally:
            await self._safety_net()
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
        """One page of history. FloodWaits are slept through and retried; if Pyrofork's
        high-level parser chokes on a poison message we fall back to our own raw parser."""
        attempt = 0
        while True:
            try:
                page = []
                async for m in client.get_chat_history(chat_id, limit=size, offset_id=offset_id):
                    page.append(m)
                return page
            except FloodWait as e:
                attempt += 1
                secs = min(int(getattr(e, "value", None) or getattr(e, "seconds", 30)), 600)
                await _log(self.job_id,
                           f"Telegram flood limit on read — sleeping {secs}s, then continuing "
                           f"(wait #{attempt}). Progress is safe.", "warn")
                await asyncio.sleep(secs + 1)
            except Exception as e:  # noqa: BLE001
                attempt += 1
                if attempt > 2:
                    await _log(self.job_id,
                               f"High-level parser failed on a page near #{offset_id} ({e}); "
                               "switching this page to the safe raw parser.", "warn")
                    return await self._raw_page(client, chat_id, offset_id, size)
                await asyncio.sleep(2)

    async def _raw_page(self, client, chat_id, offset_id: int, size: int) -> list:
        """Fetch a page via raw MTProto and convert to lightweight shims that the
        classifier understands. Never touches Pyrofork's Message binding, so it
        cannot hit its parse bugs."""
        while True:
            try:
                peer = await client.resolve_peer(chat_id)
                r = await client.invoke(GetHistory(
                    peer=peer, offset_id=offset_id, offset_date=0, add_offset=0,
                    limit=size, max_id=0, min_id=0, hash=0))
                users = {u.id: u for u in getattr(r, "users", []) or []}
                return [_raw_to_shim(m, users) for m in (r.messages or [])]
            except FloodWait as e:
                secs = min(int(getattr(e, "value", None) or getattr(e, "seconds", 30)), 600)
                await _log(self.job_id, f"Flood limit on raw read — sleeping {secs}s…", "warn")
                await asyncio.sleep(secs + 1)



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
        skipped = job.get("skipped") or 0
        last_id = job.get("last_msg_id") or 0
        last_logged = processed

        async for page in self._pages(client, source, last_id, page_size=500):
            for msg in page:
                processed += 1
                mtype = classify.message_type(msg)
                text = classify.message_text(msg)
                if not classify.should_copy(mtype, text, self.filters):
                    skipped += 1  # outside the user's selection → not stored
                    continue
                batch.append({
                    "msg_id": msg.id,
                    "ts": msg.date.timestamp() if msg.date else 0,
                    "type": mtype,
                    "sender": classify.message_sender(msg),
                    "size": classify.message_size(msg),
                    "urls": classify.extract_links(text),
                    "preview": classify.message_preview(msg),
                })
            await db.insert_index_rows(self.job_id, batch)
            batch.clear()
            last_id = page[-1].id
            await db.update_job(self.job_id, processed=processed, skipped=skipped, last_msg_id=last_id)
            await asyncio.sleep(0.1)  # gentle pacing — avoids triggering read flood limits
            if processed - last_logged >= 2000:
                tot = job.get("total") or 0
                await _log(self.job_id, f"Indexed {processed:,}{' / ' + f'{tot:,}' if tot else ''} messages")
                last_logged = processed
            if await self._check_stop():
                return

        await db.update_job(self.job_id, processed=processed, skipped=skipped)
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


def _raw_to_shim(m, users: dict):
    """Convert a raw MTProto message into a SimpleNamespace exposing exactly the
    attributes the classifier/report code reads."""
    o = SimpleNamespace(
        id=m.id, text=None, caption=None, photo=None, video=None, video_note=None,
        animation=None, audio=None, voice=None, document=None, sticker=None,
        poll=None, location=None, venue=None, contact=None, service=None,
        from_user=None, date=None,
    )
    o.date = datetime.fromtimestamp(m.date, tz=timezone.utc) if getattr(m, "date", None) else None
    if getattr(m, "action", None) is not None:
        o.service = True
    txt = getattr(m, "message", None) or None
    media = getattr(m, "media", None)
    if isinstance(media, raw_types.MessageMediaPhoto) and media.photo:
        size = 0
        try:
            size = max((s.size for s in media.photo.sizes if hasattr(s, "size")), default=0)
        except Exception:  # noqa: BLE001
            pass
        o.photo = SimpleNamespace(file_size=size)
        o.caption = txt
    elif isinstance(media, raw_types.MessageMediaDocument) and media.document:
        doc = media.document
        fsize = getattr(doc, "size", 0) or 0
        fname = None
        for a in (doc.attributes or []):
            if isinstance(a, raw_types.DocumentAttributeFilename):
                fname = a.file_name
            elif isinstance(a, raw_types.DocumentAttributeAnimated):
                o.animation = SimpleNamespace(file_size=fsize, duration=0, width=0, height=0, file_name=fname)
            elif isinstance(a, raw_types.DocumentAttributeVideo):
                if getattr(a, "round_message", False):
                    o.video_note = SimpleNamespace(file_size=fsize, duration=getattr(a, "duration", 0),
                                                   length=getattr(a, "w", 0))
                else:
                    o.video = SimpleNamespace(file_size=fsize, duration=getattr(a, "duration", 0),
                                              width=getattr(a, "w", 0), height=getattr(a, "h", 0),
                                              file_name=fname, supports_streaming=False)
            elif isinstance(a, raw_types.DocumentAttributeAudio):
                if getattr(a, "voice", False):
                    o.voice = SimpleNamespace(file_size=fsize, duration=getattr(a, "duration", 0))
                else:
                    o.audio = SimpleNamespace(file_size=fsize, duration=getattr(a, "duration", 0),
                                              title=getattr(a, "title", None),
                                              performer=getattr(a, "performer", None), file_name=fname)
            elif isinstance(a, raw_types.DocumentAttributeSticker):
                o.sticker = SimpleNamespace(file_size=fsize)
        if not any([o.video, o.video_note, o.animation, o.audio, o.voice, o.sticker]):
            o.document = SimpleNamespace(file_size=fsize, file_name=fname)
        o.caption = txt
    elif isinstance(media, raw_types.MessageMediaPoll):
        o.poll = SimpleNamespace(
            question=media.poll.question,
            options=[SimpleNamespace(text=x.text) for x in (media.poll.answers or [])])
    elif isinstance(media, raw_types.MessageMediaContact):
        o.contact = SimpleNamespace(phone_number=media.phone_number, first_name=media.first_name,
                                    last_name=media.last_name or None)
    elif isinstance(media, raw_types.MessageMediaGeo):
        g = media.geo
        o.location = SimpleNamespace(latitude=getattr(g, "lat", 0), longitude=getattr(g, "long", 0))
    if o.text is None and txt and not o.caption:
        o.text = txt
    fid = getattr(m, "from_id", None)
    if isinstance(fid, raw_types.PeerUser) and getattr(fid, "user_id", None) in users:
        u = users[fid.user_id]
        o.from_user = SimpleNamespace(id=u.id, first_name=getattr(u, "first_name", None),
                                      last_name=getattr(u, "last_name", None),
                                      username=getattr(u, "username", None))
    return o


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
