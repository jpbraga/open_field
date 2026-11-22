"""Create, edit and delete service configs through the API (the web UI's service editor).

Each service is a YAML file in {config}/services/. A save is validated exactly like a reload, written atomically,
named after the service id, and followed by a reload. Two things stay on the openField machine itself
(requests through localhost): services that run commands (shell, builtin), and anything that references the
daemon's secrets, so a remote admin key cannot run commands on this machine or send its secrets elsewhere.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import yaml
from pydantic import ValidationError

from .auth import read_secret
from .config import Config, ServiceSpec
from .adapters.http_job import Lazy
from .expr import render_str

LOCAL_ONLY_KINDS = {"shell", "builtin"}
SECRET_REF = re.compile(r"\bsecret\b")
FILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\.yaml")
TEMPLATES = Path(__file__).parent / "service_templates"
TEMPLATE_INFO = {
    "http_proxy": ("API proxy", "Forward an HTTP API that already runs (LM Studio, Ollama, Unsloth Studio, any "
                                "OpenAI-compatible server...) behind openField's keys and GPU slots."),
    "shell": ("Command line", "Run one or more commands per job, with parameters, input files, progress and "
                              "outputs. Only on this machine."),
    "http_job": ("Asynchronous API", "Submit a request to an API, follow it until it is done and download the "
                                     "files."),
}


class EditError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _pydantic_message(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        where = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return "; ".join(parts)


def templates() -> list[dict[str, str]]:
    out = []
    for kind, (title, description) in TEMPLATE_INFO.items():
        path = TEMPLATES / f"{kind}.yaml"
        if path.exists():
            out.append({"kind": kind, "title": title, "description": description, "text": path.read_text(),
                        "local_only": kind in LOCAL_ONLY_KINDS})
    return out


class ServiceFiles:
    def __init__(self, config: Config, running_services: Callable[[], set[str]]):
        self.config = config
        self.running_services = running_services

    @property
    def dir(self) -> Path:
        return self.config.root / "services"

    def _path(self, name: str) -> Path:
        if not FILE_NAME.fullmatch(name or ""):
            raise EditError(400, "invalid file name")
        return self.dir / name

    def _spec_of(self, path: Path) -> Optional[ServiceSpec]:
        return next((s for s in self.config.services.values() if s.source and Path(s.source).name == path.name), None)

    def listing(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.dir.glob("*.yaml")):
            spec = self._spec_of(f)
            out.append({"file": f.name, "id": spec.id if spec else None, "kind": spec.kind if spec else None,
                        "error": self.config.errors.get(f.name)})
        return out

    def read(self, name: str, local: bool) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            raise EditError(404, "no such service file")
        text = path.read_text()
        return {"file": name, "text": text, "version": version_of(text), "error": self.config.errors.get(name),
                "editable": local or not self._remote_problem(text, None)}

    def parse(self, text: str) -> ServiceSpec:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise EditError(400, f"YAML: {e}")
        if not isinstance(data, dict):
            raise EditError(400, "the file must be a YAML mapping (id: ..., name: ..., kind: ...)")
        data.pop("source", None)
        try:
            spec = ServiceSpec(**data)
            spec.check()
        except ValidationError as e:
            raise EditError(400, _pydantic_message(e))
        except ValueError as e:
            raise EditError(400, str(e))
        for r in spec.resources:
            if r not in self.config.daemon.resources:
                raise EditError(400, f"unknown resource {r!r} (declare it in daemon.yaml); known: "
                                     f"{', '.join(self.config.daemon.resources) or 'none'}")
        return spec

    @staticmethod
    def _remote_problem(text: str, spec: Optional[ServiceSpec]) -> Optional[str]:
        data = _load(text)
        kind = spec.kind if spec else (data.get("kind") if isinstance(data, dict) else None)
        if kind in LOCAL_ONLY_KINDS:
            return ("services that run commands (shell, builtin) can only be created, changed or removed on the "
                    "openField machine itself (http://localhost)")
        if _uses_secrets(data, text):
            return ("services that use the daemon's secrets can only be created, changed or removed on the openField "
                    "machine itself (http://localhost)")
        return None

    def _target(self, spec: ServiceSpec, current: Optional[Path]) -> Path:
        target = self.dir / f"{spec.id}.yaml"
        other = self.config.services.get(spec.id)
        if other and other.source and (current is None or Path(other.source).name != current.name):
            raise EditError(409, f"the id {spec.id!r} is already used by {Path(other.source).name}")
        if target.exists() and (current is None or target.name != current.name):
            raise EditError(409, f"{target.name} already exists")
        return target

    def validate(self, text: str, name: Optional[str], local: bool) -> dict[str, Any]:
        try:
            spec = self.parse(text)
            current = self._path(name) if name else None
            target = self._target(spec, current)
            if not local:
                problem = self._remote_problem(text, spec) or (
                    self._remote_problem(current.read_text(), None) if current and current.exists() else None)
                if problem:
                    raise EditError(403, problem)
        except EditError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "id": spec.id, "name": spec.name, "kind": spec.kind, "file": target.name,
                "renames": bool(name and target.name != name)}

    def _check_idle(self, *ids: Optional[str]) -> None:
        busy = self.running_services() & {i for i in ids if i}
        if busy:
            raise EditError(409, f"{', '.join(sorted(busy))} has a job running; change it when the job ends")

    def save(self, text: str, name: Optional[str], version: Optional[str], local: bool) -> ServiceSpec:
        spec = self.parse(text)
        current = self._path(name) if name else None
        current_text = None
        if current is not None:
            if not current.exists():
                raise EditError(404, "no such service file")
            current_text = current.read_text()
            if version and version != version_of(current_text):
                raise EditError(409, "the file changed since you opened it; reopen it to see the current version")
        if not local:
            problem = self._remote_problem(text, spec) or (self._remote_problem(current_text, None)
                                                            if current_text is not None else None)
            if problem:
                raise EditError(403, problem)
        old = self._spec_of(current) if current else None
        self._check_idle(spec.id, old.id if old else None)
        target = self._target(spec, current)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, target)
        if current is not None and current != target:
            current.unlink(missing_ok=True)
        self.config.load()
        return self.config.services.get(spec.id) or spec

    def delete(self, name: str, local: bool) -> Optional[str]:
        path = self._path(name)
        if not path.exists():
            raise EditError(404, "no such service file")
        if not local:
            problem = self._remote_problem(path.read_text(), None)
            if problem:
                raise EditError(403, problem)
        spec = self._spec_of(path)
        self._check_idle(spec.id if spec else None)
        path.unlink()
        self.config.load()
        return spec.id if spec else None

    def test_proxy(self, spec: ServiceSpec) -> dict[str, Any]:
        """Is the upstream answering? One GET to its client base URL, with the configured auth headers."""
        if spec.kind != "http_proxy" or not spec.proxy:
            raise EditError(400, "only proxy services can be tested this way")
        tail = spec.proxy.client_base.strip("/")
        url = spec.proxy.upstream.rstrip("/") + (f"/{tail}" if tail else "/")
        headers = {}
        if spec.proxy.upstream_auth == "headers":
            secrets_cfg = self.config.daemon.secrets
            names = {"secret": Lazy(list(secrets_cfg), lambda k: read_secret(secrets_cfg[k]))}
            try:
                headers = {k: render_str(v, names) for k, v in spec.proxy.headers.items()}
            except Exception as e:  # a missing secret: report it rather than fail
                return {"ok": False, "url": url, "error": f"headers: {e}"}
        start = time.monotonic()
        try:
            r = httpx.get(url, headers=headers, timeout=5, follow_redirects=False)
        except httpx.HTTPError as e:
            return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__}
        return {"ok": True, "url": url, "status": r.status_code, "ms": round((time.monotonic() - start) * 1000)}


def _load(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


# Free text read by people and agents only; every other string may be a template or an expression.
PROSE = {("name",), ("description",), ("agent_notes",)}


def _uses_secrets(data: Any, text: str) -> bool:
    """Does the service reference `secret` in a template or expression? Comments and prose do not count."""
    if not isinstance(data, dict):  # unparsable: look at the lines that are not comments
        return any(SECRET_REF.search(line.split("#", 1)[0]) for line in text.splitlines())

    def walk(value: Any, path: tuple) -> bool:
        if isinstance(value, dict):
            return any(walk(v, path + (str(k),)) for k, v in value.items())
        if isinstance(value, list):
            return any(walk(v, path) for v in value)
        if isinstance(value, str):
            if path in PROSE or (len(path) == 3 and path[0] == "params" and path[2] == "description"):
                return False
            return bool(SECRET_REF.search(value))
        return False

    return walk(data, ())
