from pathlib import Path

from starlette.requests import Request

from openfield.auth import add_key, is_loopback, read_keys_file


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


def test_keys_file(tmp_path: Path):
    path = tmp_path / "keys.json"
    k1 = add_key(path, "admin")
    k2 = add_key(path, "pc")
    items = read_keys_file(path)
    assert [i["name"] for i in items] == ["admin", "pc"] and {i["key"] for i in items} == {k1, k2}
    assert k1.startswith("of-") and len(k1) > 30
    assert oct(path.stat().st_mode & 0o777) == "0o600"
