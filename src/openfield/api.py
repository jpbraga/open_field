"""The daemon's HTTP surface: REST API (/api/v1), proxies (/proxy/<service>/...), MCP (/mcp), web UI (/)."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUpload
from starlette.routing import Route

from . import __version__
from .adapters.http_proxy import Proxy
from .agent_docs import connect_info, proxy_base, render_skill
from .auth import COOKIE, ROLES, Auth, KeyStoreError
from .config import Config
from .db import DB
from .media import gpu_name
from .mcp_server import build as build_mcp, current_client
from .jobs import JobManager
from .params import schema

log = logging.getLogger("openfield.api")
WEB = Path(__file__).parent / "web"


class State:
    def __init__(self, config_root: Path, port: Optional[int] = None):
        self.config = Config(config_root).load()
        self.port = port or self.config.daemon.port
        for name, err in self.config.errors.items():
            log.error("service config %s ignored: %s", name, err)
        self.db = DB(self.config.data_dir / "openfield.db")
        self.auth = Auth(self.config, self.db)
        self.manager = JobManager(self.config, self.db)
        self.proxy = Proxy(self.manager)

    def output_urls(self, job: dict) -> dict[str, str]:
        base = self.config.public_url(self.port)
        return {o["name"]: f"{base}/api/v1/jobs/{job['id']}/outputs/{o['name']}" for o in job.get("outputs") or []}

    def service_info(self, spec) -> dict[str, Any]:
        ok, why = self.manager.availability(spec)
        return {"id": spec.id, "name": spec.name, "category": spec.category, "kind": spec.kind,
                "description": spec.description, "model": spec.model, "available": ok, "unavailable_reason": why,
                "resources": spec.resources, "lock": spec.lock, "params": schema(spec),
                "outputs": [{"name": o.name, "media_type": o.media_type} for o in spec.outputs],
                "notes": spec.agent_notes or None,
                "proxy_path": f"/proxy/{spec.id}/" if spec.kind == "http_proxy" else None,
                "proxy_base_url": proxy_base(self.config.public_url(self.port), spec) if spec.kind == "http_proxy" else None}


class MCPAuth:
    """ASGI wrapper: the MCP endpoint takes the same keys as the REST API."""

    def __init__(self, app, state: State):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            who = self.state.auth.agent_client(request)
            if not who:
                await JSONResponse({"error": "send X-Token or Authorization: Bearer with an accepted key"},
                                   status_code=401)(scope, receive, send)
                return
            token = current_client.set(who.label)
            try:
                await self.app(scope, receive, send)
            finally:
                current_client.reset(token)
            return
        await self.app(scope, receive, send)


def create_app(config_root: Path, port: Optional[int] = None) -> FastAPI:
    state = State(config_root, port)
    mcp = build_mcp(state)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                      max_request_body_size=256 * 1024**2,
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    mcp_endpoint = next(r.endpoint for r in mcp_app.routes if getattr(r, "path", None) == "/mcp")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state.manager.startup()
        log.info("openField %s: %d services (%s); config errors: %s", __version__,
                 len(state.config.services), ", ".join(state.config.services), state.config.errors or "none")
        log.info("web UI: http://localhost:%d/ (no key from this machine) and %s/ (key needed)",
                 state.port, state.config.public_url(state.port))
        if not state.auth.keys():
            log.warning("no access keys yet: other machines cannot connect; create one in the web UI (Acesso) "
                        "or with `openfield key --new NAME`")
        async with mcp.session_manager.run():
            yield
        state.manager.shutdown()
        await state.proxy.close()

    app = FastAPI(title="openField", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.openfield = state
    app.router.routes.append(Route("/mcp", endpoint=MCPAuth(mcp_endpoint, state)))

    def who(request: Request) -> str:
        return state.auth.require(request).label

    def admin(request: Request) -> str:
        return state.auth.require_admin(request).label

    # ------------------------------------------------------------ open

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "gpu": gpu_name(), "services": len(state.config.services),
                "config_errors": state.config.errors or None}

    @app.post("/api/v1/login")
    async def login(request: Request, response: Response) -> dict[str, Any]:
        body = await request.json()
        token = state.auth.login(str(body.get("key", "")))
        if not token:
            raise HTTPException(401, "this key is not accepted")
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                            max_age=int(state.config.daemon.auth.session_days * 86400))
        session = state.db.session(token)
        return {"ok": True, "client": session["client"] if session else None}

    @app.post("/api/v1/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(COOKIE)
        if token:
            state.db.delete_session(token)
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ------------------------------------------------------------ authenticated

    @app.get("/api/v1/me")
    def me(request: Request) -> dict[str, Any]:
        c = state.auth.require(request)
        return {"client": c.label, "local": c.local, "role": c.role, "admin": c.admin, "key_id": c.key_id}

    @app.get("/api/v1/status")
    def status(request: Request) -> dict[str, Any]:
        who(request)
        return state.manager.status()

    @app.get("/api/v1/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        who(request)
        return state.manager.settings()

    @app.put("/api/v1/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        client = admin(request)
        body = await request.json()
        for name, values in ((body or {}).get("resources") or {}).items():
            if "slots" in (values or {}):
                try:
                    slots = int(values["slots"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "slots must be an integer")
                state.manager.set_slots(name, slots, client)
        return state.manager.settings()

    @app.get("/api/v1/services")
    def services(request: Request) -> dict[str, Any]:
        who(request)
        return {"services": [state.service_info(s) for s in state.config.services.values()],
                "config_errors": state.config.errors or None}

    @app.get("/api/v1/services/{service_id}")
    def service(service_id: str, request: Request) -> dict[str, Any]:
        who(request)
        spec = state.config.services.get(service_id)
        if not spec:
            raise HTTPException(404, "no such service")
        return state.service_info(spec)

    @app.post("/api/v1/jobs", status_code=202)
    async def create_job(request: Request) -> dict[str, Any]:
        client = who(request)
        ctype = request.headers.get("content-type", "")
        uploads: dict[str, list] = {}
        if ctype.startswith("multipart/form-data"):
            form = await request.form(max_files=64, max_fields=64)
            try:
                body = json.loads(form.get("job") or "{}")
            except ValueError:
                raise HTTPException(400, "the 'job' field must be JSON: {\"service\": ..., \"params\": {...}}")
            for key in set(form.keys()) - {"job"}:
                files = [v for v in form.getlist(key) if isinstance(v, StarletteUpload) and v.filename]
                if files:
                    uploads[key] = files
        else:
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(400, "send JSON {\"service\": ..., \"params\": {...}} or multipart with a 'job' field")
        if not isinstance(body, dict) or "service" not in body:
            raise HTTPException(400, "missing 'service'")
        spec = state.config.services.get(str(body["service"]))
        if not spec:
            raise HTTPException(404, f"unknown service {body['service']!r}; see GET /api/v1/services")
        job = await run_in_threadpool(state.manager.submit, spec, body.get("params") or {}, uploads, client)
        return {**state.manager.brief(job), "params": job["params"]}

    @app.get("/api/v1/jobs")
    def list_jobs(request: Request, state_filter: Optional[str] = None, service: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
        who(request)
        jobs, total = state.db.list_jobs(state=state_filter or request.query_params.get("state"), service=service,
                                         limit=max(1, min(limit, 500)), offset=max(0, offset))
        return {"total": total, "jobs": [state.manager.brief(state.manager.get(j["id"]) or j) for j in jobs]}

    @app.get("/api/v1/jobs/{jid}")
    def get_job(jid: str, request: Request) -> dict[str, Any]:
        who(request)
        job = state.manager.get(jid)
        if not job:
            raise HTTPException(404, "no such job")
        return {**state.manager.public(job), "output_urls": state.output_urls(job)}

    @app.post("/api/v1/jobs/{jid}/cancel")
    def cancel_job(jid: str, request: Request) -> dict[str, Any]:
        who(request)
        if not state.manager.cancel(jid):
            raise HTTPException(409, "the job is not running")
        return {"ok": True}

    @app.delete("/api/v1/jobs/{jid}")
    async def delete_job(jid: str, request: Request) -> dict[str, Any]:
        who(request)
        if not await run_in_threadpool(state.manager.delete, jid):
            raise HTTPException(404, "no such job")
        return {"ok": True}

    @app.get("/api/v1/jobs/{jid}/outputs/{name}")
    def job_output(jid: str, name: str, request: Request, download: bool = False):
        who(request)
        job = state.manager.get(jid)
        if not job:
            raise HTTPException(404, "no such job")
        found = state.manager.output_path(job, name)
        if not found:
            raise HTTPException(404 if job["state"] == "done" else 409, "no such output (or removed by retention)")
        path, media = found
        filename = f"{job['service']}_{jid[:8]}_{name}{path.suffix}"
        return FileResponse(path, media_type=media, filename=filename,
                            content_disposition_type="attachment" if download else "inline")

    @app.get("/api/v1/jobs/{jid}/inputs/{param}/{index}")
    def job_input(jid: str, param: str, index: int, request: Request):
        who(request)
        job = state.manager.get(jid)
        path = state.manager.input_path(job, param, index) if job else None
        if not path:
            raise HTTPException(404, "no such input")
        return FileResponse(path)

    @app.get("/api/v1/requests")
    def requests_log(request: Request, service: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
        who(request)
        return {"requests": state.db.list_requests(service, max(1, min(limit, 1000)))}

    @app.post("/api/v1/admin/reload")
    def reload(request: Request) -> dict[str, Any]:
        admin(request)
        old = state.config.daemon.model_dump()
        state.config.load()
        if state.config.daemon.model_dump() != old:
            log.warning("daemon.yaml changed: network and data settings apply after a restart")
        return {"services": sorted(state.config.services), "config_errors": state.config.errors or None}

    # ------------------------------------------------------------ access keys and agent onboarding

    @app.get("/api/v1/keys")
    def list_keys(request: Request) -> dict[str, Any]:
        admin(request)
        return {"keys": state.auth.records(), "roles": list(ROLES)}

    async def _body(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "send a JSON object")
        if not isinstance(body, dict):
            raise HTTPException(400, "send a JSON object")
        return body

    def _own_key(key_id: str) -> None:
        item = state.auth.by_id(key_id)
        if not item:
            raise HTTPException(404, "no such key")
        if item.get("source") != "openfield":
            raise HTTPException(400, "this key comes from oMLX: manage it in oMLX's settings")

    @app.post("/api/v1/keys", status_code=201)
    async def create_key(request: Request) -> dict[str, Any]:
        by = admin(request)
        body = await _body(request)
        try:
            record, key = state.auth.create_key(body.get("name", ""), body.get("role") or "agent", by)
        except KeyStoreError as e:
            raise HTTPException(409 if "exists" in str(e) else 400, str(e))
        return {**record, "key": key}

    @app.patch("/api/v1/keys/{key_id}")
    async def update_key(key_id: str, request: Request) -> dict[str, Any]:
        c = state.auth.require_admin(request)
        body = await _body(request)
        _own_key(key_id)
        changes = {k: body[k] for k in ("name", "role", "disabled") if k in body}
        if "disabled" in changes and not isinstance(changes["disabled"], bool):
            raise HTTPException(400, "disabled must be true or false")
        if c.key_id == key_id and (changes.get("disabled") or changes.get("role") == "agent"):
            raise HTTPException(409, "you are signed in with this key: change it from another admin key or from "
                                     "the openField machine (localhost)")
        try:
            return state.auth.update_key(key_id, c.label, **changes)
        except KeyStoreError as e:
            raise HTTPException(409 if "exists" in str(e) else 400, str(e))

    @app.delete("/api/v1/keys/{key_id}")
    def delete_key(key_id: str, request: Request) -> dict[str, Any]:
        c = state.auth.require_admin(request)
        _own_key(key_id)
        if c.key_id == key_id:
            raise HTTPException(409, "you are signed in with this key: revoke it from another admin key or from "
                                     "the openField machine (localhost)")
        state.auth.delete_key(key_id, c.label)
        return {"ok": True}

    @app.get("/api/v1/connect")
    def connect(request: Request) -> dict[str, Any]:
        who(request)
        return connect_info(state.config, state.port, state.manager.availability)

    @app.get("/api/v1/skill")
    def skill(request: Request, download: bool = False):
        who(request)
        text = render_skill(state.config, state.port, state.manager.availability, gpu_name())
        headers = {"Content-Disposition": 'attachment; filename="SKILL.md"'} if download else {}
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)

    # ------------------------------------------------------------ proxies

    @app.api_route("/proxy/{service_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(service_id: str, path: str, request: Request):
        client = who(request)
        spec = state.config.services.get(service_id)
        if not spec or spec.kind != "http_proxy":
            raise HTTPException(404, f"no proxy service {service_id!r}")
        ok, why = state.manager.availability(spec)
        if not ok:
            raise HTTPException(503, f"{service_id} is not available: {why}")
        return await state.proxy.handle(request, spec, path, client)

    # ------------------------------------------------------------ web UI

    @app.get("/", include_in_schema=False)
    def index():
        # The logo is inlined (its colour follows the theme); each copy gets its own gradient id, because a
        # gradient defined inside a hidden element does not paint elsewhere.
        logo = (WEB / "logo-inline.svg").read_text().strip()
        parts = (WEB / "index.html").read_text().split("<!--LOGO-->")
        html = parts[0] + "".join(logo.replace("sun-ui", f"sun-ui-{i}") + part for i, part in enumerate(parts[1:], 1))
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
