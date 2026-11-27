"""Loopback-only FastAPI app for live runs and local capture playback."""

import asyncio
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
from secrets import compare_digest
from subprocess import SubprocessError
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console

from rewind.approvals import BrowserApproval, UI_APPROVER
from rewind.config import RewindConfig
from rewind.events import RunEvent, final_event
from rewind.flagsmith_ops import FlagsmithOps
from rewind.orchestrator import run_check
from rewind.report import write_report
from rewind.report.data import redact_text
from rewind.request import interpret, refusal
from rewind.web.inventory import flag_inventory
from rewind.web.replay import STATIC, page, replay_files
from rewind.web.runs import LiveRun

COOKIE = "rewind_session"
POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src 'self'; "
          "form-action 'self'; frame-ancestors 'none'")


class UserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "decline"]


class Stop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["stop"]


def _run_live(state: LiveRun, config: RewindConfig, root: Path, text: str) -> None:
    try:
        state.observe(RunEvent("progress", {"text": "Interpreting your request…"}))
        selection = interpret(config, text)
        if selection is None:
            state.finish(RunEvent("final", {"verdict": "Request declined", "text": refusal(config)}))
            return
        result = run_check(config, root, request=selection, observer=state.observe,
                           input_fn=BrowserApproval(state.wait_for_gate), approver=UI_APPROVER,
                           console=Console(file=StringIO(), color_system=None))
        report, data = write_report(result.evidence, allowed_flags=config.allowed.flags)
        state.finish(final_event(data, "report"), report)
    except Exception as exc:
        state.finish(RunEvent("final", {"verdict": "Stopped", "text": "Could not finish the run.",
                                        "error": redact_text(str(exc))}))


def create_app(root: Path, repository: Path, token: str, port: int, *, config: RewindConfig | None = None,
               replay_case: str = "a", state: LiveRun | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    state = state or LiveRun()
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    replay = replay_files(repository) if config is None else {}
    token_bytes = token.encode()
    replay_reports = {f"/{name}" for name in replay if name.endswith(".html")}

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        host = request.headers.get("host")
        origin = request.headers.get("origin")
        if host not in hosts or len(request.headers.getlist("host")) != 1:
            return Response("Forbidden", status_code=403)
        if ((origin is not None or request.method != "GET") and
                (origin != f"http://{host}" or len(request.headers.getlist("origin")) != 1)):
            return Response("Forbidden", status_code=403)
        if request.method == "GET" and request.url.path.startswith("/open/"):
            provided = request.url.path.removeprefix("/open/")
            if not compare_digest(provided.encode(), token_bytes):
                return Response("Forbidden", status_code=403)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, token, httponly=True, samesite="lax")
        elif compare_digest(request.cookies.get(COOKIE, "").encode(), token_bytes):
            response = await call_next(request)
        else:
            return Response("Forbidden", status_code=403)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path != "/report" and request.url.path not in replay_reports:
            response.headers["Content-Security-Policy"] = POLICY
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return Response('{"detail":"Invalid Rewind request."}', status_code=422, media_type="application/json")

    @app.get("/")
    def index():
        return HTMLResponse(page("live" if config is not None else "replay", replay_case))

    @app.get("/api/flags")
    def flags():
        if config is None:
            raise HTTPException(404)
        try:
            return {"flags": flag_inventory(config, FlagsmithOps(config))}
        except (RuntimeError, ValueError, OSError, SubprocessError):
            raise HTTPException(502, "Could not read Flagsmith. Check the CLI login and environment configuration.") from None

    @app.post("/api/run")
    def start(body: UserRequest):
        if config is None:
            raise HTTPException(404)
        if not state.start(lambda: _run_live(state, config, root, body.text)):
            raise HTTPException(409, "A run is already active or the event stream is disconnected.")
        return {"accepted": True}

    @app.post("/api/decision")
    def decide(body: Decision):
        if config is None:
            raise HTTPException(404)
        accepted = state.decide(body.gate_id, body.decision)
        if accepted is None:
            raise HTTPException(409, "This approval is no longer pending.")
        return {"accepted": accepted}

    @app.post("/api/stop")
    def stop(body: Stop):
        if config is None:
            raise HTTPException(404)
        state.stop()
        return {"accepted": True}

    @app.get("/api/events")
    def events():
        if config is None:
            raise HTTPException(404)

        async def stream():
            state.subscribe()
            cursor = 0
            try:
                yield ": connected\n\n"
                while True:
                    for cursor, event in state.messages(cursor):
                        yield f"id: {cursor}\ndata: {json.dumps(asdict(event))}\n\n"
                    await asyncio.sleep(0.2)
            finally:
                state.disconnect()

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/report")
    def report():
        with state.lock:
            path = state.report
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, media_type="text/html")

    @app.get("/{asset}")
    def asset(asset: str):
        if asset in {"app.js", "styles.css"}:
            return FileResponse(STATIC / asset)
        if asset in replay:
            return Response(replay[asset], media_type="text/javascript" if asset.endswith(".js") else "text/html")
        raise HTTPException(404)

    return app
