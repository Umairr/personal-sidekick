"""Web server for the Sidekick. Run with:  uv run server.py   then open http://localhost:8765"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)  # must run before LangChain is imported, or tracing stays off

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from sidekick import Sidekick  # noqa: E402

ROOT = Path(__file__).parent
SANDBOX = ROOT / "sandbox"
WEB = ROOT / "web"


class AppState:
    sidekick: Sidekick | None = None
    ready = False
    error: str | None = None
    lock = asyncio.Lock()


app_state = AppState()


async def boot():
    app_state.ready, app_state.error = False, None
    try:
        sk = Sidekick(str(SANDBOX))
        await sk.setup()
        app_state.sidekick = sk
        app_state.ready = True
    except Exception as exc:  # npx missing, no network, bad key…
        app_state.error = f"{type(exc).__name__}: {exc}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    asyncio.create_task(boot())
    yield
    if app_state.sidekick:
        await app_state.sidekick.cleanup()


app = FastAPI(title="Personal Sidekick", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


class RunRequest(BaseModel):
    message: str
    success_criteria: str = ""


class ResumeRequest(BaseModel):
    decisions: list[dict]


def sse(events):
    async def stream():
        async with app_state.lock:
            try:
                async for event in events:
                    yield f"data: {json.dumps(event, default=str)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': f'{type(exc).__name__}: {exc}'})}\n\n"
                yield f"data: {json.dumps({'type': 'status', 'state': 'idle'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def require_ready() -> Sidekick:
    if not app_state.ready or not app_state.sidekick:
        raise HTTPException(503, app_state.error or "Sidekick is still starting up")
    if app_state.lock.locked():
        raise HTTPException(409, "Sidekick is busy with another step")
    return app_state.sidekick


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
async def status():
    sk = app_state.sidekick
    return {
        "ready": app_state.ready,
        "error": app_state.error,
        "busy": app_state.lock.locked(),
        "pending": sk.pending if sk else [],
        "tools": sk.tool_catalog() if sk and app_state.ready else [],
        "project": os.getenv("LANGSMITH_PROJECT") if os.getenv("LANGSMITH_TRACING") == "true" else None,
    }


@app.post("/api/run")
async def run(req: RunRequest):
    sk = require_ready()
    if sk.paused:
        raise HTTPException(409, "Answer the pending approval first")
    if not req.message.strip():
        raise HTTPException(400, "Message is empty")
    return sse(sk.run(req.message, req.success_criteria))


@app.post("/api/resume")
async def resume(req: ResumeRequest):
    sk = require_ready()
    if not sk.paused:
        raise HTTPException(409, "Nothing is waiting for approval")
    return sse(sk.resume(req.decisions))


@app.post("/api/reset")
async def reset():
    if app_state.lock.locked():
        raise HTTPException(409, "Wait for the current step to finish")
    if app_state.sidekick:
        await app_state.sidekick.cleanup()
    app_state.sidekick = None
    asyncio.create_task(boot())
    return {"ok": True}


@app.get("/api/files")
async def files():
    items = []
    for p in sorted(SANDBOX.rglob("*")):
        if p.is_file() and p.name != ".gitkeep":
            items.append({"path": str(p.relative_to(SANDBOX)), "size": p.stat().st_size,
                          "modified": p.stat().st_mtime})
    return sorted(items, key=lambda x: -x["modified"])


@app.get("/api/files/{path:path}")
async def file(path: str):
    target = (SANDBOX / path).resolve()
    if not target.is_relative_to(SANDBOX.resolve()) or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, filename=target.name)


if __name__ == "__main__":
    port = int(os.getenv("SIDEKICK_PORT", "8765"))
    print(f"\n  Sidekick → http://localhost:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port)
