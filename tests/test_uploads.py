import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from test_auth import app_config, clients  # noqa: F401  (fixture and helper)

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _keys(local):
    a = local.post("/api/v1/keys", json={"name": "a"}).json()["key"]
    b = local.post("/api/v1/keys", json={"name": "b"}).json()["key"]
    return {"X-Token": a}, {"X-Token": b}


def test_upload_resume_and_checks(app_config: Path):  # noqa: F811
    local, remote = clients(app_config)
    with local:
        ha, hb = _keys(local)
        data = bytes(range(256)) * 4096  # 1 MiB
        sha = hashlib.sha256(data).hexdigest()
        up = remote.post("/api/v1/uploads", headers=ha, json={"filename": "../clip.bin", "size": len(data), "sha256": sha}).json()
        assert up["filename"] == "clip.bin" and up["state"] == "open"
        assert up["upload_url"] == f"http://10.0.0.5:8765/api/v1/uploads/{up['id']}"
        url = f"/api/v1/uploads/{up['id']}"
        # first piece, then a piece at the wrong offset, then the rest
        r = remote.put(url, headers={**ha, "Content-Range": f"bytes 0-299999/{len(data)}"}, content=data[:300000])
        assert r.json()["received"] == 300000 and r.json()["state"] == "open"
        r = remote.put(url, headers={**ha, "Content-Range": f"bytes 0-9/{len(data)}"}, content=data[:10])
        assert r.status_code == 409 and r.json()["detail"]["received"] == 300000
        r = remote.put(url, headers={**ha, "Content-Range": f"bytes 300000-{len(data) - 1}/{len(data)}"}, content=data[300000:])
        assert r.json()["state"] == "complete" and r.json()["sha256"] == sha
        assert remote.put(url, headers=ha, content=b"x").status_code == 409  # already complete
        # someone else's upload does not exist for them; the admin (localhost) sees it
        assert remote.get(url, headers=hb).status_code == 404
        assert remote.put(url, headers=hb, content=b"x").status_code == 404
        assert [u["id"] for u in remote.get("/api/v1/uploads", headers=hb).json()["uploads"]] == []
        assert up["id"] in [u["id"] for u in local.get("/api/v1/uploads").json()["uploads"]]

        # declared size and checksum are enforced
        small = remote.post("/api/v1/uploads", headers=ha, json={"filename": "s.bin", "size": 4}).json()
        assert remote.put(f"/api/v1/uploads/{small['id']}", headers=ha, content=b"12345").status_code == 413
        bad = remote.post("/api/v1/uploads", headers=ha, json={"filename": "b.bin", "sha256": "0" * 64}).json()
        r = remote.put(f"/api/v1/uploads/{bad['id']}", headers=ha, content=b"hello")
        assert r.status_code == 422 and remote.get(f"/api/v1/uploads/{bad['id']}", headers=ha).json()["received"] == 0
        # one-step multipart
        r = remote.post("/api/v1/uploads", headers=ha, files={"file": ("m.bin", b"abc")})
        assert r.status_code == 201 and r.json()["state"] == "complete" and r.json()["size"] == 3
        assert remote.delete(f"/api/v1/uploads/{r.json()['id']}", headers=ha).json() == {"ok": True}
        assert remote.post("/api/v1/uploads", headers=ha, json={"filename": "x", "size": 10**15}).status_code == 413


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_job_from_upload_and_ranged_download(app_config: Path, tmp_path: Path):  # noqa: F811
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    local, remote = clients(app_config)
    with local:
        ha, hb = _keys(local)
        up = remote.post("/api/v1/uploads", headers=ha, json={"filename": "v.mp4"}).json()
        assert remote.put(f"/api/v1/uploads/{up['id']}", headers=ha, content=video.read_bytes()).json()["state"] == "complete"
        job = {"service": "media-probe", "uploads": {"file": up["id"]}}
        assert remote.post("/api/v1/jobs", headers=hb, json=job).status_code == 404  # not b's upload
        assert remote.post("/api/v1/jobs", headers=ha, json={**job, "uploads": {"nope": up["id"]}}).status_code == 400
        r = remote.post("/api/v1/jobs", headers=ha, json=job)
        assert r.status_code == 202, r.text
        jid = r.json()["id"]
        for _ in range(50):
            j = remote.get(f"/api/v1/jobs/{jid}", headers=ha).json()
            if j["state"] != "running":
                break
            time.sleep(0.1)
        assert j["state"] == "done", j
        stored = app_config.parent / "data" / "jobs" / jid / j["inputs"]["file"][0]["file"]
        assert stored.stat().st_nlink == 2  # a hard link to the upload, not a copy
        # the upload is still there for another job
        assert remote.get(f"/api/v1/uploads/{up['id']}", headers=ha).json()["state"] == "complete"

        # MCP: create_upload hands back the URL and the curl command
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "create_upload", "arguments": {"filename": "big.mov"}}}
        r = remote.post("/mcp", headers={**ha, "Accept": "application/json, text/event-stream"}, json=rpc)
        out = json.loads(r.json()["result"]["content"][0]["text"])
        assert out["ok"] and out["send"].startswith("curl -fsS -T 'big.mov'") and out["upload_url"] in out["send"]
