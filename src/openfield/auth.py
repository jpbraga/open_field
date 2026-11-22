"""Who is calling.

- From this machine through a loopback address (localhost, 127.0.0.1, ::1): no key needed.
- From another machine: X-Token or Authorization: Bearer with an accepted key (agents), or the session
  cookie the web UI gets by logging in with one.

Accepted keys come from oMLX's settings (when present) and from openField's own keys file.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .config import Config, SecretSpec, expand
from .db import DB

log = logging.getLogger("openfield.auth")
COOKIE = "openfield_session"
LOCAL_CLIENT = "local"
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def is_loopback(request: Request) -> bool:
    """True for a request this machine sent to a loopback address — and not one a web page from another
    origin made the browser send (the Host and Origin checks stop CSRF and DNS rebinding)."""
    peer = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if not (ip.is_loopback or (mapped is not None and mapped.is_loopback)):
        return False
    host = urlsplit("//" + (request.headers.get("host") or "")).hostname or ""
    if host not in LOOPBACK_NAMES:
        return False
    origin = request.headers.get("origin")
    if origin and (urlsplit(origin).hostname or "") not in LOOPBACK_NAMES:
        return False
    return True


class Auth:
    def __init__(self, config: Config, db: DB):
        self.config = config
        self.db = db
        self._cache: tuple[tuple, dict[str, str]] = ((), {})

    @property
    def keys_file(self) -> Path:
        return self.config.data_dir / "keys.json"

    def keys(self) -> dict[str, str]:
        """key -> client name: oMLX's API key ("admin") and sub-keys, plus openField's own keys."""
        omlx = expand(self.config.daemon.auth.omlx_settings)
        stamp = tuple(p.stat().st_mtime if p.exists() else 0 for p in (omlx, self.keys_file))
        if stamp == self._cache[0] and self._cache[1]:
            return self._cache[1]
        keys: dict[str, str] = {}
        try:
            if omlx.exists():
                auth = json.loads(omlx.read_text()).get("auth", {})
                if isinstance(auth.get("api_key"), str) and auth["api_key"]:
                    keys[auth["api_key"]] = "admin"
                for sub in auth.get("sub_keys", []):
                    if isinstance(sub, dict) and isinstance(sub.get("key"), str) and sub["key"]:
                        keys[sub["key"]] = sub.get("name") or "sub-key"
        except (OSError, ValueError) as e:
            log.error("cannot read the oMLX keys: %s", e)
        for item in read_keys_file(self.keys_file):
            keys[item["key"]] = item.get("name") or "key"
        self._cache = (stamp, keys)
        return keys

    def check_key(self, given: Optional[str]) -> Optional[str]:
        if not given:
            return None
        for key, name in self.keys().items():
            if hmac.compare_digest(given.encode(), key.encode()):
                return name
        return None

    @staticmethod
    def presented_key(request: Request) -> Optional[str]:
        token = request.headers.get("x-token")
        if token:
            return token.strip()
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            return authz[7:].strip()
        return None

    def agent_client(self, request: Request) -> Optional[str]:
        """A key, or a loopback request (no cookies: for the API, proxies and MCP)."""
        name = self.check_key(self.presented_key(request))
        if name:
            return name
        if self.config.daemon.auth.loopback_without_key and is_loopback(request):
            return LOCAL_CLIENT
        return None

    def client(self, request: Request) -> Optional[str]:
        who = self.agent_client(request)
        if who:
            return who
        cookie = request.cookies.get(COOKIE)
        if cookie:
            name = self.db.session_client(cookie)
            if name:
                return f"{name} (web)"
        return None

    def require(self, request: Request) -> str:
        who = self.client(request)
        if not who:
            if not self.keys():
                raise HTTPException(503, "no access keys exist yet: run `openfield key` on the openField machine")
            raise HTTPException(401, "send X-Token (or Authorization: Bearer) with an accepted key")
        return who

    def login(self, key: str) -> Optional[str]:
        name = self.check_key(key)
        if not name:
            return None
        token = secrets.token_urlsafe(32)
        self.db.add_session(token, name, self.config.daemon.auth.session_days)
        return token


def read_keys_file(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [k for k in data.get("keys", []) if isinstance(k, dict) and isinstance(k.get("key"), str) and k["key"]]


def add_key(path: Path, name: str) -> str:
    """Create a named key in openField's keys file (mode 0600) and return it."""
    items = read_keys_file(path)
    key = "of-" + secrets.token_urlsafe(24)
    items.append({"name": name, "key": key})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"keys": items}, indent=1))
    if os.name != "nt":
        tmp.chmod(0o600)
    os.replace(tmp, path)
    return key


def read_secret(spec: SecretSpec) -> Optional[str]:
    try:
        value: Any = json.loads(Path(expand(spec.file)).read_text())
        for part in spec.json_path.split("."):
            value = value[part]
        return value if isinstance(value, str) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
