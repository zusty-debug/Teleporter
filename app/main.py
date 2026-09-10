"""Teleporter — Telegram channel/group migration & indexing web app.

Entry point: FastAPI app serving the API + static frontend.
Run locally:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router
from .config import APP_NAME, APP_VERSION, HOST, PORT
from .tg import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("teleporter")


CLEANUP_AFTER_SECONDS = 30 * 60  # finished jobs are auto-deleted 30 minutes later


async def recover_zombie_jobs() -> None:
    """A server restart kills in-flight engine tasks; mark their jobs failed so
    the UI offers Resume instead of a frozen 'Indexing' state."""
    conn = await db.connect()
    try:
        cur = await conn.execute(
            "SELECT id FROM jobs WHERE status IN ('pending','indexing','migrating')")
        ids = [r["id"] for r in await cur.fetchall()]
        for jid in ids:
            await conn.execute(
                "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                ("Server restarted while the job was running. "
                 "Press Resume to continue from the checkpoint.", time.time(), jid))
        if ids:
            await conn.commit()
            log.info("Recovered %d zombie job(s): %s", len(ids), ids)
    finally:
        await conn.close()


async def cleanup_loop() -> None:
    """Delete finished jobs (and their index data/reports) 30 minutes after completion."""
    while True:
        await asyncio.sleep(30)
        try:
            conn = await db.connect()
            try:
                cur = await conn.execute(
                    "SELECT id FROM jobs WHERE status IN ('done','failed','canceled') "
                    "AND updated_at < ?", (time.time() - CLEANUP_AFTER_SECONDS,))
                ids = [r["id"] for r in await cur.fetchall()]
            finally:
                await conn.close()
            for jid in ids:
                await db.delete_job_data(jid)
                log.info("Auto-cleaned job %s (30-minute retention)", jid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("cleanup loop error: %s", e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    await recover_zombie_jobs()
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        profile = await manager.restore_saved_session()
        if profile:
            log.info("Restored Telegram session: %s", profile.get("name"))
        else:
            log.info("No saved Telegram session — waiting for login from the UI.")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not restore saved session: %s", e)
    log.info("%s v%s ready on %s:%s", APP_NAME, APP_VERSION, HOST, PORT)
    yield
    cleanup_task.cancel()
    try:
        if manager.client is not None:
            await manager.client.disconnect()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=__file__.replace("main.py", "static")), name="static")


@app.get("/")
async def index():
    import os
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT)
