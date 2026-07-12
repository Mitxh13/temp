"""
main.py
-------
FastAPI service that accepts telemetry / event-log JSON and returns a
human-readable report generated with a local llama.cpp server running
Llama-3.2-3B-Instruct (GGUF, Q4_K_M).

Run the llama.cpp server first, e.g.:
    ./llama-server -m Llama-3.2-3B-Instruct-Q4_K_M.gguf -c 4096 --port 8080

Then run this service:
    python main.py
    (or: uvicorn main:app --host 0.0.0.0 --port 8000)

Endpoints:
    GET  /health        -> service + llama.cpp reachability check
    POST /analyze        -> body: {"path": "/abs/or/relative/path.json", "source": "optional label"}
                             reads a JSON log file that already sits on disk
    POST /analyze/json   -> body: the telemetry JSON itself, sent directly
                             (useful when another system POSTs the log straight to you)

Both endpoints accept an optional query param `plain=true` to get back a
plain-text report instead of a JSON envelope.

Note: browser-style multipart file upload (FastAPI's UploadFile/File) needs
the extra "python-multipart" package, which isn't in the minimal-libs list
this was built against -- so uploads are deliberately done as a JSON body
with a file path instead, which needs nothing beyond fastapi + pydantic.
If python-multipart is available on your machine, adding a multipart
/analyze/upload route is a small, optional addition.
"""

import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError

from models import TelemetryPayload
from report_generator import check_llm_server, generate_report

load_dotenv()

LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "120"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "700"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))

app = FastAPI(
    title="Telemetry Report Generator",
    description=(
        "Reads telemetry event-log JSON and produces a human-readable report "
        "using a local llama.cpp + Llama-3.2-3B-Instruct server."
    ),
    version="1.0.0",
)


@app.get("/health")
def health():
    llm_up = check_llm_server(LLAMA_SERVER_URL, timeout=5.0)
    return {
        "service": "ok",
        "llama_cpp_server": LLAMA_SERVER_URL,
        "llama_cpp_reachable": llm_up,
    }


class AnalyzeFileRequest(BaseModel):
    path: str
    source: Optional[str] = None


def _run_report(payload: TelemetryPayload, source: str):
    return generate_report(
        payload=payload,
        source=source,
        llm_base_url=LLAMA_SERVER_URL,
        timeout=REQUEST_TIMEOUT_S,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
    )


@app.post("/analyze")
def analyze_file(
    req: AnalyzeFileRequest,
    plain: bool = Query(False, description="Return plain text instead of JSON"),
):
    """Reads a telemetry JSON log file that already exists on this machine."""
    if not os.path.isfile(req.path):
        raise HTTPException(status_code=404, detail="File not found: {0}".format(req.path))

    try:
        with open(req.path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Could not read file: {0}".format(exc))

    try:
        payload = TelemetryPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    result = _run_report(payload, req.source or req.path)

    if plain:
        return PlainTextResponse(result["report"])
    return JSONResponse(result)


@app.post("/analyze/json")
def analyze_json(
    payload: TelemetryPayload,
    source: str = Query("inline JSON body", description="Label for the source"),
    plain: bool = Query(False, description="Return plain text instead of JSON"),
):
    """Accepts the telemetry JSON directly as the request body."""
    result = _run_report(payload, source)

    if plain:
        return PlainTextResponse(result["report"])
    return JSONResponse(result)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
