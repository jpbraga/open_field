"""MCP server (streamable HTTP at /mcp) with tools for agents: services, jobs, status, outputs."""
from __future__ import annotations

import base64
import binascii
import contextvars
import io
import time
from typing import TYPE_CHECKING, Any, Optional, Union

import anyio
from fastapi import HTTPException, UploadFile
from mcp.server.mcpserver import Image, MCPServer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .params import schema
from .uploads import UploadError

if TYPE_CHECKING:
    from .api import State
    from .auth import Client

current_client: contextvars.ContextVar[str] = contextvars.ContextVar("openfield_client", default="mcp")
current_identity: contextvars.ContextVar[Optional["Client"]] = contextvars.ContextVar("openfield_identity", default=None)
INLINE_IMAGE_MAX = 4 * 1024**2  # larger images come back as a download URL, like videos

INSTRUCTIONS = """openField: local generation and media services on the user's Mac (M5 Max). The GPU has a
user-set number of slots (1 by default) and every job also needs enough free memory for its model:
submit_job answers busy (status 409, with retry_after_s) when no slot is free or the memory would not fit next
to the running jobs; wait and retry then. status() shows the slots in use. Typical flow: list_services ->
submit_job -> wait_job (repeat until done) -> get_output. Video generation is slow (minutes to tens of
minutes); image generation takes about a minute.
Large files never go inside a tool call: create_upload gives an upload URL that carries its own token, you run
the curl command it returns from your shell (no key or environment variable needed; resumable), then
submit_job(..., uploads={"<file param>": "<upload id>"}). Large outputs come back from get_output as a signed
download URL (no key needed; resumable with curl -C -) with their SHA-256. On Windows PowerShell, call
curl.exe (plain `curl` is an alias for Invoke-WebRequest there)."""


class FileInput(BaseModel):
    param: str = Field(description="Name of the file parameter, e.g. 'images' or 'first_frame'")
    filename: str = Field(description="File name with extension, e.g. 'photo.png'")
    data_base64: str = Field(description="The file content, base64-encoded")


def _http_error(e: HTTPException) -> dict[str, Any]:
    detail = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
    out = {"ok": False, "status": e.status_code, **detail}
    if e.headers and "Retry-After" in e.headers:
        out["retry_after_s"] = int(e.headers["Retry-After"])
    return out


def build(state: "State") -> MCPServer:
    m = MCPServer("openfield", title="openField", instructions=INSTRUCTIONS, version=__version__)

    def service_info(spec) -> dict[str, Any]:
        ok, why = state.manager.availability(spec)
        return {"id": spec.id, "name": spec.name, "category": spec.category, "kind": spec.kind,
                "description": spec.description, "model": spec.model, "available": ok, "unavailable_reason": why,
                "params": schema(spec), "outputs": [o.name for o in spec.outputs] or None}

    @m.tool(description="GPU slots in use and free (busy = no free slot), the running jobs with progress, ETA and "
                        "model state, free memory, and the last finished job.")
    def status() -> dict[str, Any]:
        return state.manager.status()

    @m.tool(description="The services you can run (image generation and editing, video generation, video "
                        "upscale/interpolation, LLM proxies...) with their parameter schemas.")
    def list_services() -> list[dict[str, Any]]:
        return [service_info(s) for s in state.config.services.values()]

    def base() -> str:
        return state.config.public_url(state.port)

    def who() -> "Client":
        from .auth import LOCAL
        return current_identity.get() or LOCAL

    @m.tool(description="Start a job. params follow the service's schema (see list_services). File parameters: "
                        "large files (videos, anything over a few MB) go first through create_upload and are "
                        "passed here as `uploads` {param: upload_id or [ids]}; small files may go in `files` as "
                        "base64. Returns the job id, or ok=false with status 409 (busy, with retry_after_s), 503 "
                        "(not enough memory / unavailable) or 400 (invalid parameters).")
    async def submit_job(service: str, params: Optional[dict[str, Any]] = None,
                         files: Optional[list[FileInput]] = None,
                         uploads: Optional[dict[str, Union[str, list[str]]]] = None) -> dict[str, Any]:
        spec = state.config.services.get(service)
        if not spec:
            return {"ok": False, "status": 404, "error": f"unknown service {service!r}",
                    "services": sorted(state.config.services)}
        try:
            staged = state.staged_uploads(spec, uploads, who())
        except HTTPException as e:
            return _http_error(e)
        uploads_in: dict[str, list] = {k: list(v) for k, v in staged.items()}
        for f in files or []:
            try:
                data = base64.b64decode(f.data_base64, validate=True)
            except (binascii.Error, ValueError):
                return {"ok": False, "status": 400, "error": f"{f.param}: data_base64 is not valid base64"}
            uploads_in.setdefault(f.param, []).append(UploadFile(file=io.BytesIO(data), filename=f.filename))
        try:
            job = await run_in_threadpool(state.manager.submit, spec, params or {}, uploads_in,
                                          f"{current_client.get()} (mcp)")
        except HTTPException as e:
            return _http_error(e)
        return {"ok": True, "job": state.manager.brief(job), "params": job["params"]}

    def transfer_commands(meta: dict[str, Any]) -> dict[str, str]:
        url, name = state.upload_url(meta["id"]), meta["filename"].replace('"', "")
        return {"upload_url": url,
                "send": f'curl -fsS -T "{name}" "{url}"',
                "resume": f'curl -fsS -C {meta["received"] or "<received>"} -T "{name}" "{url}"',
                "windows_powershell": f'curl.exe -fsS -T "{name}" "{url}"'}

    @m.tool(description="Begin sending a large input file. Returns upload_id and an upload_url that carries its own "
                        "token: run the `send` command from your shell in the folder of the file (or put the file's "
                        "full path in it) — no key or environment variable is needed. Then pass the id to "
                        "submit_job in `uploads`. An interrupted transfer resumes with get_upload's `resume` "
                        "command. Uploads stay usable for retries until they expire (expires_at).")
    def create_upload(filename: str, size_bytes: Optional[int] = None, sha256: Optional[str] = None) -> dict[str, Any]:
        c = who()
        try:
            meta = state.manager.uploads.create(filename, size_bytes, sha256, c.key_id or c.name, f"{c.label} (mcp)")
        except UploadError as e:
            return {"ok": False, "status": e.status, "error": str(e), **e.extra}
        return {"ok": True, "upload_id": meta["id"], "expires_at": meta["expires_at"], **transfer_commands(meta),
                "then": f"submit_job(service, params, uploads={{'<file param>': '{meta['id']}'}})"}

    @m.tool(description="An upload's state (open or complete), bytes received, size, SHA-256, and the commands to "
                        "send or resume it.")
    def get_upload(upload_id: str) -> dict[str, Any]:
        try:
            meta = state.manager.uploads.get(upload_id)
        except UploadError as e:
            return {"ok": False, "status": e.status, "error": str(e)}
        out = {"ok": True, **state.upload_view(meta)}
        if meta["state"] != "complete":
            out.update(transfer_commands(meta))
        return out

    @m.tool(description="Delete an upload you no longer need (they also expire on their own).")
    def delete_upload(upload_id: str) -> dict[str, Any]:
        try:
            state.manager.uploads.delete(upload_id, state.upload_owner(who()))
        except UploadError as e:
            return {"ok": False, "status": e.status, "error": str(e)}
        return {"ok": True}

    @m.tool(description="A job's state, stage, progress (0..1), ETA, outputs and error.")
    def get_job(job_id: str) -> dict[str, Any]:
        job = state.manager.get(job_id)
        if not job:
            return {"ok": False, "status": 404, "error": "no such job"}
        return {"ok": True, **state.manager.public(job), "output_urls": state.output_urls(job)}

    @m.tool(description="Wait up to timeout_s seconds (max 120) for a job to finish, then return its status. "
                        "Call it again while state is still running.")
    async def wait_job(job_id: str, timeout_s: float = 60) -> dict[str, Any]:
        deadline = time.time() + max(1.0, min(120.0, timeout_s))
        while True:
            job = state.manager.get(job_id)
            if not job:
                return {"ok": False, "status": 404, "error": "no such job"}
            if job["state"] not in ("queued", "running") or time.time() >= deadline:
                return {"ok": True, **state.manager.brief(job), "output_urls": state.output_urls(job)}
            await anyio.sleep(1.0)

    @m.tool(description="Recent jobs, newest first, optionally filtered by state (running, done, error, "
                        "cancelled) or service.")
    def list_jobs(state_filter: Optional[str] = None, service: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        jobs, total = state.manager.db.list_jobs(state=state_filter, service=service, limit=max(1, min(limit, 200)))
        return {"total": total, "jobs": [state.manager.brief(state.manager.get(j["id"]) or j) for j in jobs]}

    @m.tool(description="Stop a running job.")
    def cancel_job(job_id: str) -> dict[str, Any]:
        return {"ok": state.manager.cancel(job_id)}

    @m.tool(description="Run a failed or cancelled job again as a new job: same service, parameters (seed included) "
                        "and input files. Answers like submit_job (busy, insufficient memory...).")
    async def retry_job(job_id: str) -> dict[str, Any]:
        try:
            job = await run_in_threadpool(state.manager.retry, job_id, f"{current_client.get()} (mcp)")
        except HTTPException as e:
            return _http_error(e)
        return {"ok": True, "job": state.manager.brief(job), "params": job["params"], "retry_of": job_id}

    @m.tool(description="Fetch a finished job's output. Small images come back as an image; videos and large files "
                        "as a signed download URL (no key needed) with size, SHA-256 and a resumable curl command. "
                        "inline=false always returns the URL.")
    def get_output(job_id: str, name: Optional[str] = None, inline: bool = True) -> Any:
        job = state.manager.get(job_id)
        if not job or job["state"] != "done":
            return {"ok": False, "error": "the job is not done" if job else "no such job"}
        outputs = job.get("outputs") or []
        out = next((o for o in outputs if o["name"] == name), outputs[0] if outputs and not name else None)
        if not out:
            return {"ok": False, "error": "no such output", "outputs": [o["name"] for o in outputs]}
        found = state.manager.output_path(job, out["name"])
        if not found:
            return {"ok": False, "error": "the output file was removed (retention)"}
        path, media = found
        if inline and media.startswith("image/") and path.stat().st_size <= INLINE_IMAGE_MAX:
            return Image(data=path.read_bytes(), format=media.split("/")[1])
        url = state.output_urls(job).get(out["name"])
        filename = f"{job['service']}_{job_id[:8]}_{out['name']}{path.suffix}"
        return {"ok": True, "name": out["name"], "media_type": media, "size_bytes": out.get("size_bytes"),
                "sha256": out.get("sha256"), "url": url, "url_expires_in_h": state.config.daemon.links_hours,
                "download": f'curl -fL -C - -o "{filename}" "{url}"',
                "windows_powershell": f'curl.exe -fL -C - -o "{filename}" "{url}"'}

    return m
