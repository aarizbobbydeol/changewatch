import asyncio
import os
import sys
from pathlib import Path
from fastapi import FastAPI, HTTPException
import uvicorn

# Ensure sdk is in Python path for local execution
sdk_path = Path(__file__).resolve().parent.parent / "sdk"
if str(sdk_path) not in sys.path:
    sys.path.insert(0, str(sdk_path))

import changewatch
from changewatch import ASGIMiddleware

API_KEY = os.getenv("CHANGEWATCH_API_KEY", "cw_test_key_123")
ENDPOINT = os.getenv("CHANGEWATCH_ENDPOINT", "http://localhost:8000")
RELEASE = os.getenv("GIT_SHA", "demo_commit_sha_123")
ENVIRONMENT = os.getenv("ENVIRONMENT", "prod")

# 1. Initialize ChangeWatch SDK
cw_client = changewatch.init(
    api_key=API_KEY,
    release=RELEASE,
    environment=ENVIRONMENT,
    endpoint=ENDPOINT,
    slow_ms=1500.0,
)

app = FastAPI(title="ChangeWatch Demo App")

# 2. Add ChangeWatch ASGI Middleware
app.add_middleware(ASGIMiddleware, client=cw_client)


@app.get("/")
async def root():
    """Normal healthy route (contributes to request traffic heartbeat)."""
    return {"status": "ok", "message": "Demo app is healthy"}


@app.get("/crash")
async def crash():
    """Unhandled exception route to trigger regression error capture."""
    return 1 / 0


@app.get("/key-error")
async def key_error():
    """Unhandled KeyError crash route."""
    data = {"user_id": 100}
    return data["non_existent_key"]


@app.get("/slow")
async def slow():
    """Slow request route taking 1.6 seconds (> 1500ms slow_ms threshold)."""
    await asyncio.sleep(1.6)
    return {"status": "completed", "duration_ms": 1600}


@app.get("/manual")
async def manual_capture():
    """Manual exception capture route without crashing the response."""
    try:
        raise ValueError("Manual error captured by application logic")
    except Exception as exc:
        changewatch.capture_exception(exc, extra={"context": "manual_trigger"})
    return {"ok": True, "captured": True}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    print(f"Starting Demo App on port {port} linked to ChangeWatch at {ENDPOINT} (Release: {RELEASE})")
    uvicorn.run(app, host="0.0.0.0", port=port)
