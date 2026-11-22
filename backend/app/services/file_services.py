"""Customer file uploads: validation, chunked uploads, and where order files are stored.

Validation (every path)
    Only JPG/JPEG, PNG, WebP, and PDF files are accepted. The extension, the browser's declared type, and the
    file's first bytes (its signature) must all agree; the original name is reduced to a plain file name and
    the file is saved under a random name, so a customer can never choose where a file goes. The size limit
    depends on the product: Tarpaulin / large-format products accept bigger print files
    (``PRINTSHOP_LARGE_FORMAT_MAX_UPLOAD_MB``, default 25 MB) than everything else (``PRINTSHOP_MAX_UPLOAD_MB``,
    default 5 MB).

Chunked uploads (order design and reference files)
    ``POST /api/uploads`` starts an upload for the signed-in customer (product, upload field, name, size,
    type), ``PUT /api/uploads/{id}/chunks/{n}`` sends the file in pieces of at most 2 MiB, and
    ``POST /api/uploads/{id}/complete`` checks the size and the file signature. The order then refers to the
    upload by id. Only the customer who started an upload can send pieces, complete it, or attach it to an
    order, and only to the same product and field. Every request stays far below the 4.5 MB request limit of
    Vercel, so large files work on the deployed site. Unused uploads are forgotten after 24 hours.

Storage (``PRINTSHOP_FILE_STORAGE``)
    ``disk`` writes files to ``PRINTSHOP_UPLOAD_DIR`` (Docker volume); ``database`` keeps them in the database
    (default when Turso is configured, because Vercel's disk is temporary). Files are read back from either
    place, so switching keeps older files reachable on the host that has them. Files are only ever served
    through the authorized order-file endpoints (owner or administrator).
"""

from __future__ import annotations

import base64
import binascii
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.app.core.config import settings
from backend.app.repositories import file_repository


UPLOAD_DIR = settings.upload_dir
CHUNK_BYTES = 2 * 1024 * 1024
MAX_NAME_LENGTH = 180
# Combined size of every file of one order (at least 30 MB; three full-size files for large-format orders).
MIN_ORDER_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_OPEN_UPLOADS = 60
SESSION_HOURS = 24
ALLOWED_UPLOADS = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".webp": ("image/webp", b"RIFF"),
    ".pdf": ("application/pdf", b"%PDF-"),
}
ALLOWED_TEXT = "JPG, JPEG, PNG, WebP, or PDF"
SESSION_ID_PREFIX = "upl_"


def _bad(detail: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def megabytes(size: int) -> str:
    value = size / (1024 * 1024)
    return f"{value:.0f} MB" if value >= 10 or value.is_integer() else f"{value:.1f} MB"


def order_upload_limit_bytes(per_file: int) -> int:
    return max(MIN_ORDER_UPLOAD_BYTES, per_file * 3)


# ================================================================== validation


def clean_name(value: Any) -> tuple[str, str, str]:
    """(plain file name, lower-case extension, expected MIME type) or 422 for an unsupported file."""
    if not isinstance(value, str) or not value.strip():
        raise _bad("File name is required.")
    name = Path(value.strip().replace("\\", "/")).name.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise _bad(f"File names must be 1 to {MAX_NAME_LENGTH} characters long.")
    extension = Path(name).suffix.lower()
    allowed = ALLOWED_UPLOADS.get(extension)
    if allowed is None:
        raise _bad(f"{name}: upload a {ALLOWED_TEXT} file.")
    return name, extension, allowed[0]


def check_signature(name: str, extension: str, head: bytes) -> None:
    signature = ALLOWED_UPLOADS[extension][1]
    if not head.startswith(signature) or (extension == ".webp" and head[8:12] != b"WEBP"):
        raise _bad(f"{name} is not a valid {extension[1:].upper()} file.")


def decode_data_url(upload: Any, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    """A file sent inline as {name, dataUrl} (payment proofs, drafts, and older order clients)."""
    if not isinstance(upload, dict):
        raise _bad("Invalid file.")
    name, extension, mime_type = clean_name(upload.get("name"))
    data_url = upload.get("dataUrl")
    if not isinstance(data_url, str) or not data_url.startswith(f"data:{mime_type};base64,"):
        raise _bad(f"{name} has a file type that does not match its extension.")
    try:
        contents = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise _bad(f"{name} could not be decoded.") from error
    if not contents or len(contents) > max_bytes:
        raise _bad(f"{name} must be no larger than {megabytes(max_bytes)}.", 413)
    check_signature(name, extension, contents[:16])
    return new_metadata(name, extension, mime_type, len(contents)), contents


def new_metadata(name: str, extension: str, mime_type: str, size: int) -> dict[str, Any]:
    return {
        "id": secrets.token_hex(12),
        "name": name,
        # A random stored name: the customer's file name never becomes a path.
        "filename": f"{secrets.token_hex(16)}{extension}",
        "mime_type": mime_type,
        "size": size,
    }


# ================================================================== chunked uploads


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")


def start_upload(customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Start uploading one file for a product's upload field ("" = the order's general reference files)."""
    from backend.app.services import catalog_services

    if not isinstance(payload, dict):
        raise _bad("Upload details are required.")
    product = catalog_services.orderable_product(payload.get("product_id"))
    field_key = payload.get("field_key") or ""
    if not isinstance(field_key, str):
        raise _bad("Invalid upload field.")
    if field_key and catalog_services.file_field(product, field_key) is None:
        raise _bad("This product has no such upload field.")
    name, extension, mime_type = clean_name(payload.get("name"))
    declared = payload.get("mime_type") or ""
    if declared and declared != mime_type:
        raise _bad(f"{name} has a file type that does not match its extension.")
    size = payload.get("size")
    limit = catalog_services.upload_limit_bytes(product)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise _bad(f"{name} is empty or its size is unknown.")
    if size > limit:
        raise _bad(f"{name} is {megabytes(size)}. Files for {product['name']} must be {megabytes(limit)} or smaller.", 413)
    file_repository.purge_sessions_before(_cutoff())
    if file_repository.open_session_count(str(customer_id)) >= MAX_OPEN_UPLOADS:
        raise _bad("Too many unfinished uploads. Submit or reload your order, then try again.", 429)
    session_id = f"{SESSION_ID_PREFIX}{secrets.token_hex(16)}"
    file_repository.create_session({
        "id": session_id,
        "customer_id": str(customer_id),
        "product_id": product["id"],
        "field_key": field_key,
        "name": name,
        "extension": extension,
        "mime_type": mime_type,
        "size": size,
    })
    return {"upload_id": session_id, "chunk_size": CHUNK_BYTES, "max_bytes": limit, "name": name, "size": size}


def _owned_session(customer_id: str, upload_id: Any) -> dict[str, Any]:
    session = file_repository.get_session(upload_id) if isinstance(upload_id, str) and upload_id.startswith(SESSION_ID_PREFIX) else None
    # Another customer's upload looks exactly like a missing one.
    if session is None or not secrets.compare_digest(session["customer_id"], str(customer_id)):
        raise _bad("This upload was not found or has expired. Choose the file again.", 404)
    return session


def put_chunk(customer_id: str, upload_id: str, index: int, data: bytes) -> dict[str, Any]:
    session = _owned_session(customer_id, upload_id)
    if session["completed"]:
        raise _bad("This upload is already complete.", 409)
    if not data or len(data) > CHUNK_BYTES:
        raise _bad(f"Send the file in pieces of at most {megabytes(CHUNK_BYTES)}.", 413)
    count = session["chunk_count"]
    if index == count:
        # A new piece: every earlier piece must be full size.
        if count and session["received"] != count * CHUNK_BYTES:
            raise _bad("Only the last piece of a file may be smaller than the piece size.", 409)
        previous = 0
    elif count and index == count - 1:
        # The last piece sent again (for example after a network error).
        previous = file_repository.chunk_length(upload_id, index)
    else:
        raise _bad(f"Expected piece {count} of {session['name']}.", 409)
    if session["received"] - previous + len(data) > session["size"]:
        raise _bad(f"{session['name']} is larger than the size declared when the upload started.", 413)
    try:
        file_repository.write_chunk(upload_id, index, data, session["received"], previous)
    except sqlite3.IntegrityError as error:
        raise _bad("The upload changed while this piece was being saved. Try again.", 409) from error
    return {"upload_id": upload_id, "received": session["received"] - previous + len(data), "size": session["size"]}


def complete_upload(customer_id: str, upload_id: str) -> dict[str, Any]:
    session = _owned_session(customer_id, upload_id)
    if not session["completed"]:
        if session["received"] != session["size"]:
            raise _bad(f"{session['name']} is incomplete ({session['received']:,} of {session['size']:,} bytes). Upload it again.", 409)
        try:
            check_signature(session["name"], session["extension"], file_repository.first_bytes(upload_id, 16))
        except HTTPException:
            file_repository.delete_session(upload_id)
            raise
        file_repository.mark_completed(upload_id)
    return {"upload_id": upload_id, "name": session["name"], "size": session["size"], "mime_type": session["mime_type"], "completed": True}


def staged_upload(customer_id: str, upload_id: Any, product: Any, field_key: str, max_bytes: int) -> tuple[dict[str, Any], str]:
    """A completed upload of this customer for this product and field: (attachment metadata, upload id)."""
    session = _owned_session(customer_id, upload_id)
    if not session["completed"]:
        raise _bad(f"{session['name']} has not finished uploading. Wait for the upload, then submit again.", 409)
    if session["product_id"] != product["id"] or session["field_key"] != field_key:
        raise _bad(f"{session['name']} was uploaded for another product or field. Choose the file again.")
    if session["size"] > max_bytes:
        raise _bad(f"{session['name']} must be no larger than {megabytes(max_bytes)}.", 413)
    return new_metadata(session["name"], session["extension"], session["mime_type"], session["size"]), session["id"]


# ================================================================== storage


def _database_storage() -> bool:
    return settings.file_storage == "database"


def _split(contents: bytes) -> list[bytes]:
    return [contents[start:start + CHUNK_BYTES] for start in range(0, len(contents), CHUNK_BYTES)] or [b""]


def save(metadata: dict[str, Any], source: bytes | str) -> None:
    """Store one file under ``metadata["filename"]``. ``source`` is the file's bytes or a completed upload id."""
    filename = metadata["filename"]
    if isinstance(source, str):
        session = file_repository.get_session(source)
        if session is None or not session["completed"]:
            raise _bad(f"{metadata['name']} is no longer available. Choose the file again.", 409)
        if _database_storage():
            file_repository.adopt_session(source, filename, metadata["mime_type"], session["size"], session["chunk_count"])
            return
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with (UPLOAD_DIR / filename).open("wb") as handle:
            for chunk in file_repository.iter_chunks(source):
                handle.write(chunk)
        file_repository.delete_session(source)
        return
    if _database_storage():
        file_repository.store_chunks(filename, metadata["mime_type"], _split(source))
        return
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / filename).write_bytes(source)


def save_all(items: list[tuple[dict[str, Any], bytes | str]]) -> list[dict[str, Any]]:
    """Store every file of an order, or none of them."""
    saved: list[dict[str, Any]] = []
    try:
        for metadata, source in items:
            save(metadata, source)
            saved.append(metadata)
    except HTTPException:
        for metadata in saved:
            delete(metadata["filename"])
        raise
    except (OSError, sqlite3.Error) as error:
        for metadata in saved:
            delete(metadata["filename"])
        raise _bad("The uploaded files could not be saved.", 500) from error
    return saved


def delete(filename: str) -> None:
    if not filename:
        return
    try:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        file_repository.delete_stored_file(filename)
    except sqlite3.Error:
        pass


def _content_disposition(name: str, inline: bool) -> str:
    kind = "inline" if inline else "attachment"
    quoted = quote(name)
    if quoted != name:
        return f"{kind}; filename*=utf-8''{quoted}"
    return f'{kind}; filename="{name}"'


def response(filename: str, mime_type: str, download_name: str, *, inline: bool = False) -> Response:
    """The stored file, from the upload folder or the database; 404 when it is not available here."""
    path = UPLOAD_DIR / filename
    if filename and path.is_file():
        return FileResponse(path, media_type=mime_type, filename=download_name, content_disposition_type="inline" if inline else "attachment")
    stored = file_repository.stored_file(filename) if filename else None
    if stored is None:
        raise _bad("The stored file is unavailable.", 404)
    # Streamed one piece at a time, so large print files are never loaded into memory at once.
    return StreamingResponse(
        file_repository.iter_chunks(filename),
        media_type=mime_type,
        headers={"Content-Length": str(stored["size"]), "Content-Disposition": _content_disposition(download_name, inline)},
    )


def read_bytes(filename: str) -> bytes | None:
    path = UPLOAD_DIR / filename
    if path.is_file():
        return path.read_bytes()
    if file_repository.stored_file(filename) is None:
        return None
    return b"".join(file_repository.iter_chunks(filename))


# ================================================================== catalog (product and package) photos


CATALOG_PREFIX = "catalog/"


def save_catalog_image(filename: str, mime_type: str, contents: bytes) -> None:
    """Product and package photos follow the same storage setting as order files."""
    if _database_storage():
        file_repository.store_chunks(CATALOG_PREFIX + filename, mime_type, _split(contents))
        return
    directory = UPLOAD_DIR / "catalog"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_bytes(contents)


def catalog_image_response(filename: str, mime_type: str) -> Response:
    path = UPLOAD_DIR / "catalog" / filename
    if path.is_file():
        return FileResponse(path, media_type=mime_type)
    stored = file_repository.stored_file(CATALOG_PREFIX + filename)
    if stored is None:
        raise _bad("Image not found.", 404)
    return StreamingResponse(
        file_repository.iter_chunks(CATALOG_PREFIX + filename),
        media_type=mime_type,
        headers={"Content-Length": str(stored["size"]), "Cache-Control": "public, max-age=86400"},
    )
