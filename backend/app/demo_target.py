"""Separate real HTTP target. Read-only; there is no fault execution endpoint."""
import time
from threading import Lock
from fastapi import FastAPI, Request
app=FastAPI(title="Local demo target",docs_url=None,redoc_url=None,openapi_url=None)
started=time.monotonic()
requests=0
mutations=0
lock=Lock()
@app.middleware("http")
async def count_mutations(request: Request, call_next):
    global mutations
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        with lock:
            mutations += 1
    return await call_next(request)
@app.get("/_witness")
def witness():
    with lock:
        return {"mutation_requests": mutations, "requests": requests}
@app.get("/health")
def health():
    global requests
    with lock:
        requests+=1
        return {"status":"healthy","service":"demo-target","requests":requests,"mutations_received":mutations,"uptime_seconds":round(time.monotonic()-started,3)}
