import json
from pathlib import Path

import pytest
import yaml
from starlette.requests import Request
from starlette.testclient import TestClient

from openfield.auth import KeyStore, KeyStoreError, hash_key, is_loopback

CONFIG = Path(__file__).resolve().parents[1] / "config"


def request(peer: str, host: str, origin: str | None = None) -> Request:
    headers = [(b"host", host.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 50000),
                    "query_string": b""})


def test_loopback_is_trusted_only_when_peer_and_host_are_loopback():
    assert is_loopback(request("127.0.0.1", "localhost:8765"))
    assert is_loopback(request("127.0.0.1", "127.0.0.1:8765"))
    assert is_loopback(request("::1", "[::1]:8765"))
    assert is_loopback(request("127.0.0.1", "localhost:8765", origin="http://localhost:8765"))
    # another machine
    assert not is_loopback(request("192.168.0.50", "192.168.0.205:8765"))
    # this machine, but through its LAN address
    assert not is_loopback(request("192.168.0.205", "192.168.0.205:8765"))
    # DNS rebinding: a hostile name resolving to 127.0.0.1
    assert not is_loopback(request("127.0.0.1", "evil.example:8765"))
    # CSRF: a page from another site making the browser call localhost
    assert not is_loopback(request("127.0.0.1", "localhost:8765", origin="https://evil.example"))
    assert not is_loopback(request("not-an-ip", "localhost"))


def test_key_store_keeps_only_hashes(tmp_path: Path):
    store = KeyStore(tmp_path / "keys.json")
    rec, key = store.create("pc-sala", "agent", "test")
    assert key.startswith("of-") and len(key) > 30 and rec["prefix"] == key[:7]
    raw = (tmp_path / "keys.json").read_text()
    assert key not in raw and hash_key(key) in raw
    assert oct((tmp_path / "keys.json").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(KeyStoreError):
        store.create("PC-sala", "agent")  # names are unique, ignoring case
    with pytest.raises(KeyStoreError):
        store.create("x", "root")
    with pytest.raises(KeyStoreError):
        store.create("   ")
    assert store.update(rec["id"], role="admin", disabled=True)["role"] == "admin"
    store.delete(rec["id"])
    assert store.read() == []


def test_plain_text_keys_from_older_versions_are_migrated(tmp_path: Path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"keys": [{"name": "admin", "key": "of-legacy-key-0123456789"}]}))
    items = KeyStore(path).read()
    assert items[0]["hash"] == hash_key("of-legacy-key-0123456789") and items[0]["role"] == "admin"
    assert "of-legacy-key-0123456789" not in path.read_text()


@pytest.fixture
def app_config(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    (root / "services").mkdir(parents=True)
    omlx = tmp_path / "omlx.json"
    omlx.write_text(json.dumps({"auth": {"api_key": "omlx-main-key", "sub_keys": [{"name": "lan", "key": "omlx-sub"}]}}))
    for name in ("media-probe.yaml", "omlx.yaml", "qwen-image.yaml"):
        spec = yaml.safe_load((CONFIG / "services" / name).read_text())
        spec["requires"] = [str(omlx)]  # available on any machine
        (root / "services" / name).write_text(yaml.safe_dump(spec, allow_unicode=True))
    daemon = (CONFIG / "daemon.yaml").read_text()
    daemon = daemon.replace("data_dir: ~/.local/share/openfield", f"data_dir: {tmp_path / 'data'}")
    daemon = daemon.replace("omlx_settings: ~/.omlx/settings.json", f"omlx_settings: {omlx}")
    daemon = daemon.replace("public_url: auto", "public_url: http://10.0.0.5:8765")
    (root / "daemon.yaml").write_text(daemon)
    return root


def clients(config_root: Path):
    from openfield.api import create_app

    app = create_app(config_root, 8765)
    local = TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))
    remote = TestClient(app, base_url="http://10.0.0.5:8765", client=("10.0.0.9", 50000))
    return local, remote


def test_key_management_flow(app_config: Path):
    local, remote = clients(app_config)
    with local:  # one client runs the app lifespan; the other shares the app
        assert local.get("/api/v1/me").json()["admin"] is True
        assert remote.get("/api/v1/status").status_code == 401

        r = local.post("/api/v1/keys", json={"name": "n8n", "role": "agent"})
        assert r.status_code == 201
        agent = r.json()
        assert agent["key"].startswith("of-") and agent["role"] == "agent"
        assert local.post("/api/v1/keys", json={"name": "N8N"}).status_code == 409
        r = local.post("/api/v1/keys", json={"name": "laptop", "role": "admin"})
        admin = r.json()

        ah = {"X-Token": agent["key"]}
        # an agent key uses the API but cannot manage keys or change settings
        assert remote.get("/api/v1/status", headers=ah).status_code == 200
        assert remote.get("/api/v1/me", headers=ah).json() == {"client": "n8n", "local": False, "role": "agent",
                                                                "admin": False, "key_id": agent["id"]}
        assert remote.get("/api/v1/keys", headers=ah).status_code == 403
        assert remote.post("/api/v1/keys", headers=ah, json={"name": "evil"}).status_code == 403
        assert remote.put("/api/v1/settings", headers=ah, json={"resources": {"gpu": {"slots": 2}}}).status_code == 403
        assert remote.post("/mcp", headers={**ah, "Accept": "application/json, text/event-stream"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 200

        # an admin key manages keys; listings never carry keys or hashes
        bh = {"Authorization": f"Bearer {admin['key']}"}
        listing = remote.get("/api/v1/keys", headers=bh).json()["keys"]
        text = json.dumps(listing)
        assert agent["key"] not in text and admin["key"] not in text and "hash" not in text
        names = {(k["name"], k["source"], k["role"]) for k in listing}
        assert {("n8n", "openfield", "agent"), ("laptop", "openfield", "admin"), ("admin", "omlx", "admin"),
                ("lan", "omlx", "agent")} <= names
        assert next(k for k in listing if k["name"] == "n8n")["last_used_at"]
        omlx_id = next(k["id"] for k in listing if k["source"] == "omlx")
        assert remote.delete(f"/api/v1/keys/{omlx_id}", headers=bh).status_code == 400
        # nobody locks themselves out by accident
        assert remote.patch(f"/api/v1/keys/{admin['id']}", headers=bh, json={"disabled": True}).status_code == 409
        assert remote.delete(f"/api/v1/keys/{admin['id']}", headers=bh).status_code == 409

        # disable, enable, revoke
        assert remote.patch(f"/api/v1/keys/{agent['id']}", headers=bh, json={"disabled": True}).json()["disabled"]
        assert remote.get("/api/v1/status", headers=ah).status_code == 401
        remote.patch(f"/api/v1/keys/{agent['id']}", headers=bh, json={"disabled": False})
        assert remote.get("/api/v1/status", headers=ah).status_code == 200
        assert remote.patch(f"/api/v1/keys/{agent['id']}", headers=bh, json={"disabled": "no"}).status_code == 400
        assert remote.delete(f"/api/v1/keys/{agent['id']}", headers=bh).json() == {"ok": True}
        assert remote.get("/api/v1/status", headers=ah).status_code == 401


def test_web_session_ends_when_its_key_is_revoked(app_config: Path):
    local, remote = clients(app_config)
    with local:  # one client runs the app lifespan; the other shares the app
        key = local.post("/api/v1/keys", json={"name": "browser", "role": "admin"}).json()
        assert remote.post("/api/v1/login", json={"key": key["key"]}).status_code == 200
        assert remote.get("/api/v1/me").json()["client"] == "browser (web)"
        local.patch(f"/api/v1/keys/{key['id']}", json={"role": "agent"})
        assert remote.get("/api/v1/me").json()["admin"] is False  # the key's current role applies
        local.delete(f"/api/v1/keys/{key['id']}")
        assert remote.get("/api/v1/me").status_code == 401


def test_connect_info_and_skill(app_config: Path):
    local, remote = clients(app_config)
    with local:  # one client runs the app lifespan; the other shares the app
        key = local.post("/api/v1/keys", json={"name": "agent"}).json()["key"]
        info = remote.get("/api/v1/connect", headers={"X-Token": key}).json()
        assert info["mcp_url"] == "http://10.0.0.5:8765/mcp" and info["key_env"] == "OPENFIELD_KEY"
        assert {"id": "omlx", "base_url": "http://10.0.0.5:8765/proxy/omlx/v1"}.items() <= info["proxies"][0].items()
        r = remote.get("/api/v1/skill?download=true", headers={"X-Token": key})
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        skill = r.text
        assert skill.startswith("---\nname: openfield\ndescription: ")
        assert "http://10.0.0.5:8765" in skill and "$OPENFIELD_KEY" in skill and key not in skill
        assert "`qwen-image`" in skill and "`prompt` (string, **required**)" in skill
        assert "proxy/omlx/v1" in skill
        assert remote.get("/api/v1/skill").status_code == 401
