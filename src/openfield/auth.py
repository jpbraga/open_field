"""Who is calling: oMLX keys (X-Token or Bearer) for agents, a session cookie for the web UI."""
from __future__ import annotations

import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request

from .config import Config, SecretSpec, expand
from .db import DB

log = logging.getLogger("openfield.auth")
COOKIE = "openfield_session"


class Auth:
    def __init__(self, config: Config, db: DB):
        self.config = config
        self.db = db
        self._cache: tuple[float, dict[str, str]] = (0.0, {})

    def keys(self) -> dict[str, str]:
        """key -> client name: oMLX's API key ("admin") and its sub-keys (their own names)."""
        path = expand(self.config.daemon.auth.omlx_settings)
        try:
            mtime = path.stat().st_mtime
            if mtime != self._cache[0]:
                auth = json.loads(path.read_text()).get("auth", {})
                keys: dict[str, str] = {}
                if isinstance(auth.get("api_key"), str) and auth["api_key"]:
                    keys[auth["api_key"]] = "admin"
                for sub in auth.get("sub_keys", []):
                    if isinstance(sub, dict) and isinstance(sub.get("key"), str) and sub["key"]:
                        keys[sub["key"]] = sub.get("name") or "sub-key"
                self._cache = (mtime, keys)
        except (OSError, ValueError) as e:
            log.error("cannot read the accepted keys: %s", e)
            return {}
        return self._cache[1]

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

    def client(self, request: Request) -> Optional[str]:
        name = self.check_key(self.presented_key(request))
        if name:
            return name
        cookie = request.cookies.get(COOKIE)
        if cookie:
            who = self.db.session_client(cookie)
            if who:
                return f"{who} (web)"
        return None

    def require(self, request: Request) -> str:
        who = self.client(request)
        if not who:
            if not self.keys():
                raise HTTPException(503, "no accepted keys are configured on the Mac")
            raise HTTPException(401, "send X-Token (or Authorization: Bearer) with a key oMLX accepts")
        return who

    def login(self, key: str) -> Optional[str]:
        name = self.check_key(key)
        if not name:
            return None
        token = secrets.token_urlsafe(32)
        self.db.add_session(token, name, self.config.daemon.auth.session_days)
        return token


def read_secret(spec: SecretSpec) -> Optional[str]:
    try:
        value: Any = json.loads(Path(expand(spec.file)).read_text())
        for part in spec.json_path.split("."):
            value = value[part]
        return value if isinstance(value, str) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
