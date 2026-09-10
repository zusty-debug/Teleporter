"""FastAPI routes: auth, chat pickers, jobs, reports."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import classify, db, engine, report
from .config import APP_PASSWORD, APP_VERSION, DEFAULT_API_HASH, DEFAULT_API_ID
from .tg import AuthError, TwoFactorRequired, manager

router = APIRouter(prefix="/api")


def _guarded(request: Request) -> None:
    if not APP_PASSWORD:
        return
    token = request.headers.get("X-App-Token") or request.query_params.get("token")
    if token != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong or missing app password")


# ---------------------------------------------------------------- bootstrap / health

@router.get("/health")
async def health():
    return {"ok": True, "version": APP_VERSION}


@router.get("/bootstrap")
async def bootstrap(request: Request):
    if APP_PASSWORD:
        token = request.headers.get("X-App-Token") or request.query_params.get("token")
        if token != APP_PASSWORD:
            return {"need_password": True}
    return {
        "need_password": False,
        "authenticated": manager.profile is not None,
        "profile": manager.profile,
        "kinds": {k: {"label": v["label"], "topics": v["topics"], "index": v["index"],
                      "has_dest": v["dest"] is not None} for k, v in engine.KINDS.items()},
        "filters": [{"key": k, "label": l, "hint": h} for k, l, h in classify.FILTER_DEFS],
        "defaults": {"api_id": DEFAULT_API_ID, "api_hash": DEFAULT_API_HASH, "delay": 1.0},
        "active_jobs": [jid for jid, eng in engine.engines.items()],
    }


# ---------------------------------------------------------------- auth

class SessionLogin(BaseModel):
    api_id: int
    api_hash: str
    session_string: str


class SendCode(BaseModel):
    api_id: int
    api_hash: str
    phone: str


class VerifyCode(BaseModel):
    code: str
    password: Optional[str] = None


@router.post("/auth/session")
async def auth_session(body: SessionLogin, request: Request):
    _guarded(request)
    try:
        profile = await manager.connect_with_session(body.api_id, body.api_hash, body.session_string)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"profile": profile, "session_string": body.session_string}


@router.post("/auth/send-code")
async def auth_send_code(body: SendCode, request: Request):
    _guarded(request)
    try:
        await manager.send_code(body.api_id, body.api_hash, body.phone)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "message": f"Login code sent to {body.phone}"}


@router.post("/auth/verify-code")
async def auth_verify(body: VerifyCode, request: Request):
    _guarded(request)
    try:
        session_string, profile = await manager.verify_code(body.code, body.password)
    except TwoFactorRequired as e:
        return {"need_password": True, "message": str(e)}
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"session_string": session_string, "profile": profile}


@router.post("/auth/cancel-login")
async def auth_cancel(request: Request):
    _guarded(request)
    await manager.cancel_login()
    return {"ok": True}


@router.get("/auth/me")
async def auth_me(request: Request):
    _guarded(request)
    if manager.profile is None:
        raise HTTPException(status_code=404, detail="Not authenticated")
    return {"profile": manager.profile}


@router.post("/auth/disconnect")
async def auth_disconnect(request: Request):
    _guarded(request)
    await manager.disconnect()
    return {"ok": True}


# ---------------------------------------------------------------- chats

@router.get("/chats")
async def chats(request: Request, q: str = ""):
    _guarded(request)
    try:
        return {"chats": await manager.list_dialogs(q)}
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ResolveChat(BaseModel):
    target: str


@router.post("/chats/resolve")
async def resolve_chat(body: ResolveChat, request: Request):
    _guarded(request)
    try:
        return {"chat": await manager.resolve_chat(body.target)}
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


class TopicsReq(BaseModel):
    chat_id: int


@router.post("/chats/topics")
async def chat_topics(body: TopicsReq, request: Request):
    _guarded(request)
    try:
        return {"topics": await manager.get_topics(body.chat_id)}
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------- jobs

class JobCreate(BaseModel):
    kind: str = Field(..., description="c2c|c2g|g2c|g2g|c2t|g2t|index")
    source: str
    dest: Optional[str] = None
    filters: list = Field(default_factory=lambda: ["all"])
    delay: float = 1.0
    start_now: bool = True


@router.post("/jobs")
async def create_job(body: JobCreate, request: Request):
    _guarded(request)
    if body.kind not in engine.KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown operation '{body.kind}'")
    if body.delay < 0 or body.delay > 60:
        raise HTTPException(status_code=400, detail="delay must be between 0 and 60 seconds")
    spec = engine.KINDS[body.kind]

    if not manager.profile:
        raise HTTPException(status_code=400, detail="Authenticate with Telegram first.")

    # Validate content filters
    valid = {k for k, _, _ in classify.FILTER_DEFS}
    filters = [f for f in (body.filters or []) if f in valid]
    if not filters:
        raise HTTPException(status_code=400, detail="Select at least one content type to copy.")

    try:
        source = await manager.resolve_chat(body.source)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=f"Source: {e}")
    err = engine.validate_kind_chat(body.kind, "source", source)
    if err:
        raise HTTPException(status_code=400, detail=err)

    dest = None
    if spec["dest"] is not None:
        if not body.dest:
            raise HTTPException(status_code=400, detail="Destination is required for this operation.")
        try:
            dest = await manager.resolve_chat(body.dest)
        except AuthError as e:
            raise HTTPException(status_code=400, detail=f"Destination: {e}")
        err = engine.validate_kind_chat(body.kind, "dest", dest)
        if err:
            raise HTTPException(status_code=400, detail=err)
        if dest["id"] == source["id"]:
            raise HTTPException(status_code=400, detail="Source and destination are the same chat.")

    job = {
        "id": engine.new_job_id(),
        "kind": body.kind,
        "phase": "index" if spec["index"] else "migrate",
        "status": "pending",
        "source": source,
        "dest": dest,
        "filters": filters,
        "mapping": None,
        "delay": body.delay,
    }
    await db.create_job(job)
    if body.start_now:
        engine.start_job(job)
    return {"job_id": job["id"]}


@router.get("/jobs")
async def list_jobs(request: Request):
    _guarded(request)
    jobs = await db.list_jobs()
    for j in jobs:
        j["running"] = j["id"] in engine.engines
        j["kind_label"] = engine.KINDS.get(j["kind"], {}).get("label", j["kind"])
    return {"jobs": jobs}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    _guarded(request)
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["running"] = job_id in engine.engines
    job["kind_label"] = engine.KINDS.get(job["kind"], {}).get("label", job["kind"])
    job["logs"] = await db.get_logs(job_id, limit=120)
    if job["status"] == "awaiting_mapping":
        job["type_counts"] = await db.index_type_counts(job_id)
    return {"job": job}


async def _active_engine(job_id: str) -> engine.Engine:
    eng = engine.engines.get(job_id)
    if eng is None:
        raise HTTPException(status_code=409, detail="This job is not currently running.")
    return eng


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str, request: Request):
    _guarded(request)
    eng = await _active_engine(job_id)
    eng.request_pause()
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    _guarded(request)
    eng = engine.engines.get(job_id)
    if eng is not None:
        eng.request_cancel()
    else:
        await db.update_job(job_id, status="canceled")
    return {"ok": True}


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: Request):
    _guarded(request)
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["id"] in engine.engines:
        raise HTTPException(status_code=409, detail="Job is already running.")
    if job["status"] not in ("paused", "failed", "canceled"):
        raise HTTPException(status_code=409, detail=f"Cannot resume a job with status '{job['status']}'.")
    if job["status"] == "failed" and not job.get("last_msg_id"):
        pass  # allow retry from start
    await engine.resume_job(job)
    return {"ok": True}


class MappingBody(BaseModel):
    mapping: dict


@router.post("/jobs/{job_id}/mapping")
async def apply_mapping(job_id: str, body: MappingBody, request: Request):
    _guarded(request)
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "awaiting_mapping":
        raise HTTPException(status_code=409, detail="This job is not waiting for a topic mapping.")
    mapping = body.mapping or {}
    if not mapping.get("types"):
        raise HTTPException(status_code=400, detail="Mapping must include per-type topic assignments.")
    await db.update_job(job_id, mapping=mapping, status="paused", phase="migrate",
                        last_msg_id=0, processed=0, copied=0, skipped=0, failed=0)
    job = await db.get_job(job_id)
    await engine.resume_job(job)
    return {"ok": True}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    _guarded(request)
    if job_id in engine.engines:
        engine.engines[job_id].request_cancel()
    await db.delete_job_data(job_id)
    return {"ok": True}


# ---------------------------------------------------------------- reports

async def _report_data(job_id: str):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    rows = await db.get_index_rows(job_id)
    if not rows:
        raise HTTPException(status_code=404, detail="This job has no index data (it may have been a direct copy).")
    return job, rows, report.build_stats(job, rows)


@router.get("/jobs/{job_id}/report.html", response_class=HTMLResponse)
async def report_html(job_id: str, request: Request):
    _guarded(request)
    job, rows, stats = await _report_data(job_id)
    return report.to_html(job, rows, stats)


@router.get("/jobs/{job_id}/report.json")
async def report_json(job_id: str, request: Request):
    _guarded(request)
    job, rows, stats = await _report_data(job_id)
    return Response(content=report.to_json(job, rows, stats), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="teleporter-index-{job_id}.json"'})


@router.get("/jobs/{job_id}/report.csv")
async def report_csv(job_id: str, request: Request):
    _guarded(request)
    job, rows, stats = await _report_data(job_id)
    return Response(content=report.to_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="teleporter-index-{job_id}.csv"'})
