"""Local web server for the SALU desktop interface.

Security:
  * Binds to 127.0.0.1 only (enforced by config validation).
  * Every API/WebSocket call needs the per-launch token generated at startup.
  * WebSocket Origin must be SALU's own origin, so other websites open in your
    browser cannot drive SALU through localhost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..app import SaluApp
from ..logging_setup import memory_handler
from ..security.permissions import ApprovalRequest
from ..system_info import quick_stats

log = logging.getLogger("salu.web")
STATIC = Path(__file__).parent / "static"


def create_web_app(salu: SaluApp, token: str | None = None, run_checks: bool = True) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    port = salu.settings.server.port
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    state: dict = {"ws": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_checks:
            await salu.startup_checks()
        yield
        await salu.shutdown()

    app = FastAPI(title="SALU", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token

    def require_token(x_salu_token: str = Header(default="")):
        if not secrets.compare_digest(x_salu_token, token):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/state", dependencies=[Depends(require_token)])
    async def get_state():
        return {
            "version": __version__,
            "name": salu.settings.assistant_name,
            "user_name": salu.settings.user_name,
            "online": salu.online,
            "checks": [c.to_dict() for c in salu.checks],
            "brain": salu.brain_info,
            "agent": {"state": salu.agent.state, "current_task": salu.agent.current_task},
            "tools": [{"name": t.name, "description": t.description, "risk": t.risk.label, "category": t.category}
                      for t in salu.registry.all()],
        }

    @app.get("/api/stats", dependencies=[Depends(require_token)])
    async def get_stats():
        return await asyncio.to_thread(quick_stats)

    @app.get("/api/tasks", dependencies=[Depends(require_token)])
    async def get_tasks(limit: int = 30):
        return salu.memory.recent_tasks(min(limit, 200))

    @app.get("/api/logs", dependencies=[Depends(require_token)])
    async def get_logs(limit: int = 200):
        return list(memory_handler.lines)[-min(limit, 500):]

    @app.get("/api/memory", dependencies=[Depends(require_token)])
    async def get_memory(q: str = ""):
        return salu.memory.recall(q, limit=200)

    @app.delete("/api/memory/{key}", dependencies=[Depends(require_token)])
    async def delete_memory(key: str):
        return {"deleted": salu.memory.forget(key)}

    @app.get("/api/settings", dependencies=[Depends(require_token)])
    async def get_settings():
        return salu.settings.public_view()

    @app.get("/api/history", dependencies=[Depends(require_token)])
    async def get_history():
        return salu.memory.messages("default", limit=60)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, t: str = Query(default="")):
        origin = ws.headers.get("origin", "")
        if not secrets.compare_digest(t, token) or origin not in allowed_origins:
            log.warning("rejected websocket connection origin=%s", origin or "-")
            await ws.close(code=4401)
            return
        await ws.accept()
        if state["ws"] is not None:  # single active UI: the newest window takes over
            salu.broker.deny_all()
            try:
                await state["ws"].close(code=4000)
            except Exception:
                pass
        state["ws"] = ws
        send_lock = asyncio.Lock()

        async def send(kind: str, data: dict) -> None:
            async with send_lock:
                await ws.send_text(json.dumps({"type": kind, **data}, default=str))

        async def show_approval(req: ApprovalRequest) -> None:
            await send("approval_required", req.to_dict())

        salu.broker.attach(show_approval)
        log.info("UI connected")
        running: set[asyncio.Task] = set()
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "chat":
                    if salu.agent.current_task:
                        await send("error", {"message": "SALU is still working on the current task."})
                        continue
                    task = asyncio.create_task(salu.agent.handle(str(msg.get("text", ""))[:20000], send))
                    running.add(task)
                    task.add_done_callback(running.discard)
                elif kind == "approve":
                    salu.broker.resolve(str(msg.get("id")), bool(msg.get("approved")))
                elif kind == "interrupt":
                    await salu.agent.interrupt()
                elif kind == "new_session":
                    await salu.agent.reset_session()
                    await send("session_reset", {})
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket error")
        finally:
            if state["ws"] is ws:
                state["ws"] = None
                salu.broker.attach(None)
                salu.broker.deny_all()   # nobody can confirm anymore => deny
            log.info("UI disconnected")

    @app.exception_handler(Exception)
    async def on_error(request, exc):
        log.exception("unhandled API error")
        return JSONResponse({"detail": f"{type(exc).__name__}"}, status_code=500)

    return app
