from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import subprocess
import tempfile
from pathlib import Path


def file_sha256(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(block_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def page_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_doc_id(sha256: str) -> str:
    return f"doc_{sha256[:12]}"


def media_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.as_posix())
    if guessed:
        return guessed
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return "image/unknown"
    if suffix in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
        return "audio/unknown"
    if suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        return "video/unknown"
    return "application/octet-stream"


def write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", dir=path.parent.as_posix())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass


def probe_media_duration_seconds(path: Path) -> float | None:
    # ffprobe is optional. Return None if unavailable or probe fails.
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path.as_posix(),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=20).strip()
        if not out:
            return None
        return float(out)
    except Exception:
        return None
