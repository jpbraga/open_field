"""Staged uploads: large input files sent over HTTP before the job, then referenced by id.

A client creates an upload (`POST /api/v1/uploads`), sends the bytes with `PUT /api/v1/uploads/<id>` (the whole
file, or pieces with `Content-Range`, which is also how an interrupted transfer resumes), and submits the job
with `"uploads": {"<file param>": "<id>"}`. The bytes stream straight to disk; the job gets a hard link, so a
multi-gigabyte video is never copied, and the upload stays usable (for a retry after a busy answer, or for
another job) until it expires.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request

from .config import Config

UPLOAD_ID = re.compile(r"up_[0-9a-f]{32}")
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")
FLUSH_BYTES = 4 << 20


class UploadError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


class StagedFile:
    """A completed upload handed to a job (in place of a multipart file)."""

    def __init__(self, path: Path, filename: str, size: int):
        self.path, self.filename, self.size = path, filename, size


class Uploads:
    def __init__(self, config: Config):
        self.config = config
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    @property
    def dir(self) -> Path:
        return self.config.data_dir / "uploads"

    @property
    def max_bytes(self) -> int:
        return int(self.config.daemon.uploads.max_gb * 1024**3)

    # ------------------------------------------------------------ records

    def _folder(self, uid: str) -> Path:
        if not UPLOAD_ID.fullmatch(uid or ""):
            raise UploadError(404, "no such upload")
        return self.dir / uid

    def _read(self, uid: str) -> dict[str, Any]:
        try:
            return json.loads((self._folder(uid) / "meta.json").read_text())
        except (OSError, ValueError):
            raise UploadError(404, "no such upload") from None

    def _save(self, meta: dict[str, Any]) -> None:
        folder = self._folder(meta["id"])
        meta["updated_at"] = round(time.time(), 3)
        meta["expires_at"] = round(meta["updated_at"] + self.config.daemon.uploads.ttl_hours * 3600)
        tmp = folder / "meta.tmp"
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, folder / "meta.json")

    @staticmethod
    def public(meta: dict[str, Any]) -> dict[str, Any]:
        return {k: meta.get(k) for k in ("id", "filename", "size", "received", "state", "sha256", "created_at",
                                          "expires_at", "client")}

    def get(self, uid: str, owner: Optional[str] = None) -> dict[str, Any]:
        meta = self._read(uid)
        if owner is not None and meta.get("owner") != owner:
            raise UploadError(404, "no such upload")  # someone else's: do not reveal it exists
        return meta

    def list(self, owner: Optional[str] = None) -> list[dict[str, Any]]:
        out = []
        for folder in sorted(self.dir.glob("up_*")) if self.dir.exists() else []:
            try:
                meta = json.loads((folder / "meta.json").read_text())
            except (OSError, ValueError):
                continue
            if owner is None or meta.get("owner") == owner:
                out.append(meta)
        return sorted(out, key=lambda m: -m.get("created_at", 0))

    def _check_disk(self, incoming: int) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.dir).free
        floor = self.config.daemon.min_free_disk_gb * 1024**3
        if free - incoming < floor:
            raise UploadError(507, f"not enough disk space: {free / 1024**3:.1f} GB free, "
                                   f"{self.config.daemon.min_free_disk_gb:g} GB must stay free",
                              free_gb=round(free / 1024**3, 1))

    def create(self, filename: str, size: Optional[int], sha256: Optional[str], owner: str,
               client: str) -> dict[str, Any]:
        name = Path(str(filename or "")).name.strip()
        if not name or len(name) > 200:
            raise UploadError(400, "filename must be a file name of 1 to 200 characters")
        if size is not None:
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise UploadError(400, "size must be a whole number of bytes")
            if size > self.max_bytes:
                raise UploadError(413, f"larger than the upload limit ({self.config.daemon.uploads.max_gb:g} GB)")
            self._check_disk(size)
        if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(sha256)):
            raise UploadError(400, "sha256 must be 64 hex characters")
        uid = "up_" + secrets.token_hex(16)
        folder = self.dir / uid
        folder.mkdir(parents=True)
        (folder / "data").touch()
        meta = {"id": uid, "filename": name, "size": size, "received": 0, "state": "open",
                "sha256_expected": sha256.lower() if sha256 else None, "sha256": None,
                "owner": owner, "client": client, "created_at": round(time.time(), 3)}
        self._save(meta)
        return meta

    def delete(self, uid: str, owner: Optional[str]) -> None:
        self.get(uid, owner)
        if uid in self._busy:
            raise UploadError(409, "a transfer to this upload is running")
        shutil.rmtree(self._folder(uid), ignore_errors=True)

    # ------------------------------------------------------------ transfer

    async def receive(self, uid: str, owner: Optional[str], request: Request) -> dict[str, Any]:
        """Write the request body into the upload: whole (no Content-Range: starts over) or one piece."""
        self.get(uid, owner)
        with self._lock:
            if uid in self._busy:
                raise UploadError(409, "another transfer to this upload is running")
            self._busy.add(uid)
        try:
            meta = self.get(uid, owner)  # read again now that no other transfer can change it
            if meta["state"] == "complete":
                raise UploadError(409, "this upload is already complete", upload=self.public(meta))
            return await self._receive(meta, request)
        finally:
            self._busy.discard(uid)

    async def _receive(self, meta: dict[str, Any], request: Request) -> dict[str, Any]:
        data = self._folder(meta["id"]) / "data"
        header = request.headers.get("content-range")
        total: Optional[int] = meta["size"]
        if header:
            m = CONTENT_RANGE.fullmatch(header.strip())
            if not m:
                raise UploadError(400, "Content-Range must look like 'bytes START-END/TOTAL'")
            start, end = int(m.group(1)), int(m.group(2))
            if m.group(3) != "*":
                if total is not None and int(m.group(3)) != total:
                    raise UploadError(400, f"Content-Range total {m.group(3)} differs from the declared size {total}")
                total = int(m.group(3))
            if end < start:
                raise UploadError(400, "Content-Range END is before START")
            if start != meta["received"]:
                raise UploadError(409, f"this upload has {meta['received']} bytes: continue from there",
                                  received=meta["received"])
            mode = "ab"
        else:
            start, mode = 0, "wb"  # the whole file: start over
        limit = min(total, self.max_bytes) if total is not None else self.max_bytes
        if total is not None and total > self.max_bytes:
            raise UploadError(413, f"larger than the upload limit ({self.config.daemon.uploads.max_gb:g} GB)")
        self._check_disk((total - start) if total is not None else 0)
        meta["size"] = total
        written, buf, disconnected = start, bytearray(), False
        fh = open(data, mode)
        try:
            try:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > limit:
                        raise UploadError(413, f"more bytes than {'the declared size' if total is not None else 'the upload limit'}"
                                               f" ({limit} bytes)")
                    buf += chunk
                    if len(buf) >= FLUSH_BYTES:
                        await run_in_threadpool(fh.write, bytes(buf))
                        buf.clear()
            except ClientDisconnect:
                disconnected = True  # keep what arrived: the client resumes with Content-Range
            if buf:
                await run_in_threadpool(fh.write, bytes(buf))
        finally:
            fh.close()
            meta["received"] = data.stat().st_size
            self._save(meta)
        if disconnected:
            return self.public(meta)
        done = (meta["received"] == total) if total is not None else not header
        if done:
            digest = await run_in_threadpool(file_sha256, data)
            if meta.get("sha256_expected") and digest != meta["sha256_expected"]:
                data.write_bytes(b"")
                meta.update(received=0, state="open")
                self._save(meta)
                raise UploadError(422, "the file's SHA-256 does not match the declared one; send it again")
            meta.update(state="complete", sha256=digest, size=meta["received"])
            self._save(meta)
        return self.public(meta)

    async def receive_form_file(self, meta: dict[str, Any], up: Any) -> dict[str, Any]:
        """A multipart file (the one-step way, for files that fit one request)."""
        data = self._folder(meta["id"]) / "data"

        def copy() -> int:
            n = 0
            with open(data, "wb") as out:
                while chunk := up.file.read(8 << 20):
                    n += len(chunk)
                    if n > self.max_bytes:
                        raise UploadError(413, f"larger than the upload limit ({self.config.daemon.uploads.max_gb:g} GB)")
                    out.write(chunk)
            return n

        meta["received"] = await run_in_threadpool(copy)
        meta.update(size=meta["received"], state="complete", sha256=await run_in_threadpool(file_sha256, data))
        self._save(meta)
        return self.public(meta)

    # ------------------------------------------------------------ use

    def staged(self, uid: str, owner: Optional[str]) -> StagedFile:
        meta = self.get(str(uid), owner)
        if meta["state"] != "complete":
            raise UploadError(409, f"upload {uid} is not complete ({meta['received']} of {meta['size'] or '?'} bytes)",
                              received=meta["received"])
        self._save(meta)  # using it keeps it alive
        return StagedFile(self._folder(uid) / "data", meta["filename"], meta["received"])

    def purge_expired(self) -> int:
        now, n = time.time(), 0
        for meta in self.list():
            if meta.get("expires_at", 0) < now and meta["id"] not in self._busy:
                shutil.rmtree(self._folder(meta["id"]), ignore_errors=True)
                n += 1
        return n
