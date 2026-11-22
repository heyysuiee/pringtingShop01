"""Chunked uploads of order files (design files, reference images) for signed-in customers.

Each request carries at most one 2 MiB piece, so large Tarpaulin print files stay within the request-size
limits of the hosting platform (Vercel: 4.5 MB per request). See ``file_services`` for the rules.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from backend.app.core.security import require_same_origin, require_session
from backend.app.services import file_services


router = APIRouter(tags=["uploads"])


@router.post("/api/uploads", status_code=201)
def start_upload(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    return file_services.start_upload(claims["sub"], payload)


@router.put("/api/uploads/{upload_id}/chunks/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    if not 0 <= index < 10_000:
        raise HTTPException(status_code=422, detail="Invalid piece number.")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > file_services.CHUNK_BYTES):
        raise HTTPException(status_code=413, detail=f"Send the file in pieces of at most {file_services.megabytes(file_services.CHUNK_BYTES)}.")
    # Read at most one piece (plus one byte to detect an oversized body) even without a Content-Length header.
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > file_services.CHUNK_BYTES:
            raise HTTPException(status_code=413, detail=f"Send the file in pieces of at most {file_services.megabytes(file_services.CHUNK_BYTES)}.")
    # The database write runs in the thread pool, like the other (synchronous) endpoints.
    return await run_in_threadpool(file_services.put_chunk, claims["sub"], upload_id, index, bytes(body))


@router.post("/api/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    return file_services.complete_upload(claims["sub"], upload_id)
