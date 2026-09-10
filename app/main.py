"""Teleporter — Telegram channel/group migration & indexing web app.

Entry point: FastAPI app serving the API + static frontend.
Run locally:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
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
