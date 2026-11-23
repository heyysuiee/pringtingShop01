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
import contextvars
import functools
import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Generator, TypeVar
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.app.core.config import settings
from backend.app.repositories import file_repository


logger = logging.getLogger("printshop.files")

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
# Random stored names (order files and catalog images) follow this shape. Restricting orphan cleanup to
# them means an administrator's manually placed file can never be removed automatically.
STORED_NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|webp|pdf)$", re.IGNORECASE)
ORPHAN_GRACE_HOURS = 24
F = TypeVar("F", bound=Callable[..., Any])

# Files saved during one service call. If the database write that follows fails, the whole call fails and
# the files saved so far are removed, so a refused order never leaves upload or database orphans.
_saved_files: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("printshop_saved_files", default=None)


def _bad(detail: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def megabytes(size: int) -> str:
    value = size / (1024 * 1024)
    return f"{value:.0f} MB" if value >= 10 or value.is_integer() else f"{value:.1f} MB"


def order_upload_limit_bytes(per_file: int) -> int:
    return max(MIN_ORDER_UPLOAD_BYTES, per_file * 3)


def _safe_stored_key(filename: Any) -> str:
    """Only a generated file name, or ``catalog/<generated name>``, may be stored/deleted here."""
    if not isinstance(filename, str) or not filename or "\\" in filename or "\x00" in filename:
        return ""
    path = PurePosixPath(filename)
    if path.is_absolute() or len(path.parts) > 2 or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    name = path.name
    if STORED_NAME.fullmatch(name) is None:
        return ""
    if len(path.parts) == 2 and path.parts[0] != CATALOG_PREFIX.rstrip("/"):
        return ""
    return filename


def _track(filename: str) -> None:
    bucket = _saved_files.get()
    if bucket is not None:
        bucket.append(filename)


@contextmanager
def saved_files() -> Generator[list[str], None, None]:
    """Remove every file saved inside this block if the block raises, on *any* exception type."""
    parent = _saved_files.get()
    bucket: list[str] = []
    token = _saved_files.set(bucket)
    try:
        yield bucket
    except BaseException:
        for filename in reversed(bucket):
            delete(filename)
        raise
    else:
        if parent is not None:
            parent.extend(bucket)
    finally:
        _saved_files.reset(token)


def compensating_files(function: F) -> F:
    """Decorate a service that saves files before writing the database."""

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with saved_files():
            return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


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


def _expired(session: dict[str, Any]) -> bool:
    """Upload sessions older than 24 hours are unusable even if startup cleanup has not run yet."""
    try:
        return datetime.fromisoformat(session["created_at"]) < datetime.fromisoformat(_cutoff())
    except (KeyError, TypeError, ValueError):  # pragma: no cover - malformed legacy rows are treated as expired
        return True


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
    if _expired(session):
        file_repository.delete_session(upload_id)
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


def _atomic_write(target: Path, chunks: Any) -> None:
    """Publish a complete file in one rename so a concurrent backup never reads a partial upload.

    Temporary files start with a dot, and backup scanning skips dotfiles, so an in-flight write is
    never included in a backup snapshot.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def save(metadata: dict[str, Any], source: bytes | str) -> None:
    """Store one file under ``metadata["filename"]``. ``source`` is the file's bytes or a completed upload id."""
    filename = _safe_stored_key(metadata.get("filename"))
    if not filename:
        raise _bad("Invalid stored file name.")
    if isinstance(source, str):
        session = file_repository.get_session(source)
        if session is not None and _expired(session):
            file_repository.delete_session(source)
            session = None
        if session is None or not session["completed"]:
            raise _bad(f"{metadata['name']} is no longer available. Choose the file again.", 409)
        if _database_storage():
            file_repository.adopt_session(source, filename, metadata["mime_type"], session["size"], session["chunk_count"])
        else:
            target = UPLOAD_DIR / filename
            _atomic_write(target, file_repository.iter_chunks(source))
            file_repository.delete_session(source)
        _track(filename)
        return
    if _database_storage():
        file_repository.store_chunks(filename, metadata["mime_type"], _split(source))
    else:
        _atomic_write(UPLOAD_DIR / filename, (source,))
    _track(filename)


def save_all(items: list[tuple[dict[str, Any], bytes | str]]) -> list[dict[str, Any]]:
    """Store every file of an order, or none of them (including unexpected errors)."""
    with saved_files():
        saved: list[dict[str, Any]] = []
        for metadata, source in items:
            save(metadata, source)
            saved.append(metadata)
        return saved


def delete(filename: str) -> None:
    key = _safe_stored_key(filename)
    if not key:
        return
    try:
        (UPLOAD_DIR / key).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        file_repository.delete_stored_file(key)
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
    key = _safe_stored_key(CATALOG_PREFIX + filename)
    if not key:
        raise _bad("Invalid catalog image name.")
    if _database_storage():
        file_repository.store_chunks(key, mime_type, _split(contents))
    else:
        _atomic_write(UPLOAD_DIR / "catalog" / filename, (contents,))
    _track(key)


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


# ================================================================== startup cleanup


def cleanup_orphans() -> dict[str, int]:
    """Remove abandoned uploads/files left by an interrupted request or a crash.

    A grace period protects files that another request may still be writing. Only generated names that
    appear nowhere in the database are removed, so a file that was saved just before its order row was
    written is kept until the next startup after it is no longer referenced.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ORPHAN_GRACE_HOURS)).timestamp()
    result = {
        "sessions": file_repository.purge_sessions_before(_cutoff()),
        "chunks": file_repository.delete_orphan_chunks(),
        "stored_files": file_repository.delete_empty_stored_files(),
        "disk_files": 0,
    }
    referenced = file_repository.referenced_stored_names()
    directory = UPLOAD_DIR
    if directory.is_dir():
        for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                    continue
                if STORED_NAME.fullmatch(path.name) is None:
                    continue
                if path.name.lower() in referenced or path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
                result["disk_files"] += 1
            except OSError:  # pragma: no cover - cleanup is best-effort, never blocks startup
                logger.warning("Could not remove orphaned upload %s", path, exc_info=True)
        for folder in sorted((item for item in directory.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:  # only empty folders are removed
                pass
    return result
