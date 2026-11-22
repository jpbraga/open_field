"""Who is calling, and what they may do.

- From this machine through a loopback address (localhost, 127.0.0.1, ::1): no key needed; full access.
- From another machine: X-Token or Authorization: Bearer with an accepted key (agents), or the session
  cookie the web UI gets by logging in with one.

Accepted keys come from openField's own keys file and from oMLX's settings (when present). Each key has a
role: "agent" (API, MCP, proxies, jobs) or "admin" (also manages keys and system settings). openField's
own keys are stored as SHA-256 hashes: a key is shown once, when it is created.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
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
ROLES = ("agent", "admin")
KEY_PREFIX = "of-"
TOUCH_EVERY_S = 60


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


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class Client:
    name: str
    role: str = "agent"
    key_id: Optional[str] = None
    local: bool = False
    web: bool = False

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return f"{self.name} (web)" if self.web else self.name


LOCAL = Client(LOCAL_CLIENT, "admin", local=True)


class KeyStoreError(ValueError):
    """A key operation the caller asked for that cannot be done (bad name, unknown id...)."""


class KeyStore:
    """openField's own keys in {data_dir}/keys.json (mode 0600). Only hashes are kept."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> list[dict[str, Any]]:
        with self.lock:
            try:
                data = json.loads(self.path.read_text())
            except FileNotFoundError:
                return []
            except (OSError, ValueError) as e:
                log.error("cannot read %s: %s", self.path, e)
                return []
            items, migrated = [], False
            for item in data.get("keys", []) if isinstance(data, dict) else []:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("key"), str) and item["key"]:
                    # Written by an earlier version in plain text: keep the key working, drop the text. These
                    # were made at the command line by the machine's owner, so they keep full access.
                    item = {"id": _new_id(), "name": item.get("name") or "key", "role": "admin",
                            "hash": hash_key(item["key"]), "prefix": item["key"][:7],
                            "created_at": self.path.stat().st_mtime, "created_by": "migrated"}
                    migrated = True
                if not (isinstance(item.get("hash"), str) and len(item["hash"]) == 64 and item.get("id")):
                    continue
                if item.get("role") not in ROLES:
                    item["role"] = "agent"
                items.append(item)
            if migrated:
                self._write(items)
                log.info("keys file migrated: keys are now stored as hashes")
            return items

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 2, "keys": items}, indent=1))
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, self.path)

    def create(self, name: str, role: str = "agent", created_by: str = "") -> tuple[dict[str, Any], str]:
        name = _clean_name(name)
        if role not in ROLES:
            raise KeyStoreError(f"role must be one of {', '.join(ROLES)}")
        with self.lock:
            items = self.read()
            if any(i["name"].lower() == name.lower() for i in items):
                raise KeyStoreError(f"a key named {name!r} already exists")
            key = KEY_PREFIX + secrets.token_urlsafe(24)
            item = {"id": _new_id(), "name": name, "role": role, "hash": hash_key(key), "prefix": key[:7],
                    "created_at": round(time.time(), 3), "created_by": created_by, "disabled": False}
            items.append(item)
            self._write(items)
        return item, key

    def update(self, key_id: str, name: Optional[str] = None, role: Optional[str] = None,
               disabled: Optional[bool] = None) -> dict[str, Any]:
        with self.lock:
            items = self.read()
            item = next((i for i in items if i["id"] == key_id), None)
            if not item:
                raise KeyStoreError("no such key")
            if name is not None:
                name = _clean_name(name)
                if any(i["name"].lower() == name.lower() and i is not item for i in items):
                    raise KeyStoreError(f"a key named {name!r} already exists")
                item["name"] = name
            if role is not None:
                if role not in ROLES:
                    raise KeyStoreError(f"role must be one of {', '.join(ROLES)}")
                item["role"] = role
            if disabled is not None:
                item["disabled"] = bool(disabled)
            self._write(items)
            return item

    def delete(self, key_id: str) -> dict[str, Any]:
        with self.lock:
            items = self.read()
            item = next((i for i in items if i["id"] == key_id), None)
            if not item:
                raise KeyStoreError("no such key")
            self._write([i for i in items if i is not item])
            return item


def _new_id() -> str:
    return "k" + secrets.token_hex(4)


def _clean_name(name: Any) -> str:
    name = " ".join(str(name or "").split())
    if not name or len(name) > 60:
        raise KeyStoreError("the name must have 1 to 60 characters")
    return name


def public_record(item: dict[str, Any]) -> dict[str, Any]:
    """What the UI and CLI may see of a key: never the key or its hash."""
    return {k: item.get(k) for k in ("id", "name", "role", "source", "prefix", "created_at", "created_by",
                                      "disabled")}


class Auth:
    def __init__(self, config: Config, db: DB):
        self.config = config
        self.db = db
        self.store = KeyStore(config.data_dir / "keys.json")
        self._cache: tuple[tuple, dict[str, dict[str, Any]]] = ((), {})
        self._touched: dict[str, float] = {}

    @property
    def keys_file(self) -> Path:
        return self.store.path

    def invalidate(self) -> None:
        self._cache = ((), {})

    def keys(self) -> dict[str, dict[str, Any]]:
        """hash -> key record, openField's keys and oMLX's (its API key as "admin", its sub-keys by name)."""
        omlx = expand(self.config.daemon.auth.omlx_settings)
        stamp = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (omlx, self.keys_file))
        if stamp == self._cache[0] and self._cache[1]:
            return self._cache[1]
        keys: dict[str, dict[str, Any]] = {}
        try:
            if omlx.exists():
                auth = json.loads(omlx.read_text()).get("auth", {})
                if isinstance(auth.get("api_key"), str) and auth["api_key"]:
                    h = hash_key(auth["api_key"])
                    keys[h] = {"id": "omlx-" + h[:10], "name": "admin", "role": "admin", "source": "omlx"}
                for sub in auth.get("sub_keys", []):
                    if isinstance(sub, dict) and isinstance(sub.get("key"), str) and sub["key"]:
                        h = hash_key(sub["key"])
                        keys[h] = {"id": "omlx-" + h[:10], "name": sub.get("name") or "sub-key", "role": "agent",
                                   "source": "omlx"}
        except (OSError, ValueError) as e:
            log.error("cannot read the oMLX keys: %s", e)
        for item in self.store.read():
            keys[item["hash"]] = {**item, "source": "openfield"}
        self._cache = (stamp, keys)
        return keys

    def by_id(self, key_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not key_id:
            return None
        return next((k for k in self.keys().values() if k["id"] == key_id), None)

    def records(self) -> list[dict[str, Any]]:
        usage = self.db.key_usage()
        out = []
        for item in self.keys().values():
            rec = public_record(item)
            rec.update(usage.get(item["id"], {}))
            out.append(rec)
        out.sort(key=lambda r: (r["source"] != "openfield", -(r.get("created_at") or 0), r["name"]))
        return out

    def check_key(self, given: Optional[str]) -> Optional[dict[str, Any]]:
        """The record of an accepted, enabled key. The lookup is by the key's SHA-256: timing reveals nothing
        usable about the key itself."""
        if not given:
            return None
        item = self.keys().get(hash_key(given))
        if not item or item.get("disabled"):
            return None
        return item

    @staticmethod
    def presented_key(request: Request) -> Optional[str]:
        token = request.headers.get("x-token")
        if token:
            return token.strip()
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            return authz[7:].strip()
        return None

    def _touch(self, item: dict[str, Any], request: Optional[Request]) -> None:
        now = time.time()
        if now - self._touched.get(item["id"], 0) < TOUCH_EVERY_S:
            return
        self._touched[item["id"]] = now
        ip = request.client.host if request is not None and request.client else None
        try:
            self.db.touch_key(item["id"], now, ip)
        except Exception as e:  # usage stats must never break a request
            log.warning("cannot record key use: %s", e)

    def agent_client(self, request: Request) -> Optional[Client]:
        """A key, or a loopback request (no cookies: for the API, proxies and MCP)."""
        item = self.check_key(self.presented_key(request))
        if item:
            self._touch(item, request)
            return Client(item["name"], item["role"], item["id"])
        if self.config.daemon.auth.loopback_without_key and is_loopback(request):
            return LOCAL
        return None

    def client(self, request: Request) -> Optional[Client]:
        who = self.agent_client(request)
        if who:
            return who
        cookie = request.cookies.get(COOKIE)
        if cookie:
            session = self.db.session(cookie)
            # The key behind the session must still exist and be enabled; its current role applies.
            item = self.by_id(session["key_id"]) if session else None
            if item and not item.get("disabled"):
                self._touch(item, request)
                return Client(item["name"], item["role"], item["id"], web=True)
        return None

    def require(self, request: Request) -> Client:
        who = self.client(request)
        if not who:
            if not self.keys():
                raise HTTPException(503, "no access keys exist yet: create one at http://localhost:"
                                         f"{self.config.daemon.port}/ (Acesso) or with `openfield key --new NAME` "
                                         "on the openField machine")
            raise HTTPException(401, "send X-Token (or Authorization: Bearer) with an accepted key")
        return who

    def require_admin(self, request: Request) -> Client:
        who = self.require(request)
        if not who.admin:
            raise HTTPException(403, "this needs an admin key (or a request from the openField machine itself "
                                     "through localhost)")
        return who

    def login(self, key: str) -> Optional[str]:
        item = self.check_key(key)
        if not item:
            return None
        token = secrets.token_urlsafe(32)
        self.db.add_session(token, item["name"], item["id"], self.config.daemon.auth.session_days)
        self._touch(item, None)
        return token

    # ------------------------------------------------------------ management (openField keys only)

    def create_key(self, name: str, role: str, by: str) -> tuple[dict[str, Any], str]:
        item, key = self.store.create(name, role, by)
        self.invalidate()
        log.info("key %s %r (%s) created by %s", item["id"], item["name"], item["role"], by)
        return {**public_record(item), "source": "openfield"}, key

    def update_key(self, key_id: str, by: str, **changes: Any) -> dict[str, Any]:
        item = self.store.update(key_id, **changes)
        self.invalidate()
        if changes.get("disabled"):
            self.db.delete_key_sessions(key_id)
        log.info("key %s %r changed by %s: %s", key_id, item["name"], by,
                 {k: v for k, v in changes.items() if v is not None})
        return {**public_record(item), "source": "openfield"}

    def delete_key(self, key_id: str, by: str) -> None:
        item = self.store.delete(key_id)
        self.invalidate()
        self.db.delete_key_sessions(key_id)
        log.info("key %s %r revoked by %s", key_id, item["name"], by)


def read_secret(spec: SecretSpec) -> Optional[str]:
    try:
        value: Any = json.loads(Path(expand(spec.file)).read_text())
        for part in spec.json_path.split("."):
            value = value[part]
        return value if isinstance(value, str) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
