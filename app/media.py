import re
import sqlite3
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.culture import _connect
from app.publishing import _staff_from_authorization

router = APIRouter(tags=["Media"])

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 30 * 1024 * 1024
MAX_AUDIO_BYTES = 2 * 1024 * 1024

ALLOWED_TYPES = {
    "image": {"image/jpeg", "image/png", "image/webp"},
    "video": {"video/mp4", "video/webm"},
    "audio": {
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-wav",
    },
}


def initialize_media_store() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS uploaded_media (
                media_id TEXT PRIMARY KEY,
                media_kind TEXT NOT NULL CHECK (media_kind IN ('image', 'video', 'audio')),
                content_type TEXT NOT NULL,
                content BLOB NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


def _matches_media_signature(content_type: str, content: bytes) -> bool:
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if content_type in {"video/mp4", "audio/mp4"}:
        return len(content) >= 12 and content[4:8] == b"ftyp"
    if content_type in {"video/webm", "audio/webm"}:
        return content.startswith(b"\x1a\x45\xdf\xa3")
    if content_type == "audio/ogg":
        return content.startswith(b"OggS")
    if content_type in {"audio/wav", "audio/x-wav"}:
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WAVE"
    if content_type == "audio/mpeg":
        return content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
        )
    return False


def _upload_limit(media_kind: str) -> int:
    return {
        "image": MAX_IMAGE_BYTES,
        "video": MAX_VIDEO_BYTES,
        "audio": MAX_AUDIO_BYTES,
    }[media_kind]


@router.post("/api/v1/staff/media", status_code=201)
async def upload_staff_media(
    file: Annotated[UploadFile, File()],
    media_kind: Annotated[Literal["image", "video", "audio"], Form()],
    expected_content_type: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str | int]:
    staff = _staff_from_authorization(authorization)
    if not (
        staff["role"] == "admin"
        or staff["can_manage_stories"]
        or staff["house_names_json"] != "[]"
    ):
        raise HTTPException(status_code=403, detail="Your account is not authorized to upload site media")

    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_TYPES[media_kind]:
        raise HTTPException(status_code=415, detail=f"Unsupported {media_kind} file type")
    if expected_content_type and content_type != expected_content_type:
        raise HTTPException(status_code=415, detail=f"Choose a {expected_content_type} file for this media field")

    content = await file.read(_upload_limit(media_kind) + 1)
    if len(content) > _upload_limit(media_kind):
        raise HTTPException(status_code=413, detail=f"{media_kind.title()} file exceeds the upload size limit")
    if not content or not _matches_media_signature(content_type, content):
        raise HTTPException(status_code=415, detail="The uploaded file does not match its declared media type")

    media_id = str(uuid4())
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO uploaded_media (media_id, media_kind, content_type, content, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (media_id, media_kind, content_type, sqlite3.Binary(content), staff["staff_id"]),
        )
    return {
        "url": f"/api/media/{media_id}",
        "media_kind": media_kind,
        "content_type": content_type,
        "size": len(content),
    }


@router.get("/api/media/{media_id}")
def get_uploaded_media(media_id: UUID, request: Request) -> Response:
    initialize_media_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT content_type, content FROM uploaded_media WHERE media_id = ?",
            (str(media_id),),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Media file not found")

    content = bytes(row["content"])
    size = len(content)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    }
    range_header = request.headers.get("range")
    if not range_header:
        return Response(content=content, media_type=row["content_type"], headers=headers)

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise HTTPException(
            status_code=416,
            detail="Invalid byte range",
            headers={"Content-Range": f"bytes */{size}"},
        )

    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    else:
        suffix_length = int(match.group(2))
        start = max(0, size - suffix_length)
        end = size - 1
    if start >= size or end < start:
        raise HTTPException(
            status_code=416,
            detail="Byte range is not satisfiable",
            headers={"Content-Range": f"bytes */{size}"},
        )

    end = min(end, size - 1)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return Response(
        content=content[start : end + 1],
        status_code=206,
        media_type=row["content_type"],
        headers=headers,
    )
