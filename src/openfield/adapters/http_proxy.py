"""http_proxy adapter: forward requests to an upstream API (e.g. oMLX's OpenAI-compatible /v1).

Each call takes the service's resources for as long as the response streams. With `lock: shared`, calls
to the same service run side by side up to max_concurrency, while every other service is refused.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse

from ..auth import read_secret
from ..config import ServiceSpec
from ..expr import render_str
from .http_job import Lazy

if TYPE_CHECKING:
    from ..jobs import JobManager

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}


class Proxy:
    def __init__(self, manager: "JobManager"):
        self.manager = manager
        self.client = httpx.AsyncClient(timeout=None, follow_redirects=False)

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self, request: Request, spec: ServiceSpec) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        token = headers.pop("x-token", None)
        mode = spec.proxy.upstream_auth
        if mode == "passthrough":
            if token and not any(k.lower() == "authorization" for k in headers):
                headers["authorization"] = f"Bearer {token}"
        else:
            headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            if mode == "headers":
                secrets_cfg = self.manager.config.daemon.secrets
                names = {"secret": Lazy(list(secrets_cfg), lambda k: read_secret(secrets_cfg[k]))}
                headers.update({k: render_str(v, names) for k, v in spec.proxy.headers.items()})
        return headers

    async def handle(self, request: Request, spec: ServiceSpec, path: str, client_name: str) -> StreamingResponse:
        owner = f"request-{uuid.uuid4().hex[:12]}"
        started = time.time()
        rec = {"service": spec.id, "method": request.method, "path": "/" + path, "client": client_name,
               "started_at": started}
        try:
            self.manager.admit_call(spec, owner)  # 409 when another service holds the resource
        except HTTPException as e:
            reason = e.detail.get("reason") if isinstance(e.detail, dict) else str(e.detail)
            self.manager.db.add_request({**rec, "status": e.status_code, "duration_s": 0, "bytes_out": 0,
                                         "error": reason})
            raise
        released = False

        def finish(status: int | None, nbytes: int, error: str | None = None) -> None:
            nonlocal released
            if released:
                return
            released = True
            self.manager.locks.release(spec, owner)
            self.manager.db.add_request({**rec, "status": status, "duration_s": round(time.time() - started, 3),
                                         "bytes_out": nbytes, "error": error})

        try:
            body = await request.body()
            if "json" in request.headers.get("content-type", "") and body:
                try:
                    model = json.loads(body).get("model")
                    rec["model"] = str(model)[:200] if model else None
                except (ValueError, AttributeError):
                    pass
            url = spec.proxy.upstream.rstrip("/") + "/" + path.lstrip("/")
            if request.url.query:
                url += "?" + request.url.query
            upstream_req = self.client.build_request(request.method, url, headers=self._headers(request, spec),
                                                     content=body, timeout=spec.proxy.timeout_s)
            upstream = await self.client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            finish(502, 0, str(e))
            raise HTTPException(502, f"{spec.id} upstream unreachable: {e}") from None
        except BaseException as e:
            finish(None, 0, str(e))
            raise

        sent = {"n": 0}

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    sent["n"] += len(chunk)
                    yield chunk
            finally:
                await upstream.aclose()
                finish(upstream.status_code, sent["n"])

        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)
