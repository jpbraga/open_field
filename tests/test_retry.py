import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from test_auth import app_config, clients  # noqa: F401  (fixture and helper)

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _wait(client, jid):
    for _ in range(100):
        j = client.get(f"/api/v1/jobs/{jid}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {jid} still running")


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_retry_failed_job_with_its_inputs(app_config: Path, tmp_path: Path):  # noqa: F811
    marker = tmp_path / "ready"
    image = tmp_path / "dot.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=8x8", "-frames:v", "1",
                    str(image)], check=True)
    # Fails until the marker exists, then copies its input to the output.
    spec = {"id": "flaky", "name": "Flaky", "category": "utility", "kind": "shell", "resources": [],
            "params": {"text": {"type": "string", "required": True},
                       "seed": {"type": "int", "default": "random", "min": 0, "max": 2147483647},
                       "picture": {"type": "file", "accept": ["image"], "max_mb": 1}},
            "steps": [{"name": "run", "env": {"MARK": str(marker)},
                       "command": ["sh", "-c", 'test -e "$MARK" && cp "$1" "$2"', "sh", "{picture}", "{work}/out.png"]}],
            "outputs": [{"name": "out", "path": "{work}/out.png", "media_type": "image/png"}]}
    (app_config / "services" / "flaky.yaml").write_text(yaml.safe_dump(spec))
    local, _ = clients(app_config)
    with local:
        r = local.post("/api/v1/jobs", data={"job": json.dumps({"service": "flaky", "params": {"text": "hi"}})},
                       files={"picture": ("dot.png", image.read_bytes(), "image/png")})
        assert r.status_code == 202, r.text
        first = _wait(local, r.json()["id"])
        assert first["state"] == "error" and first["retryable"]
        seed = first["params"]["seed"]

        marker.touch()
        r = local.post(f"/api/v1/jobs/{first['id']}/retry")
        assert r.status_code == 202, r.text
        assert r.json()["retry_of"] == first["id"] and r.json()["id"] != first["id"]
        second = _wait(local, r.json()["id"])
        assert second["state"] == "done" and not second["retryable"]
        assert second["params"] == {"text": "hi", "seed": seed, "picture": second["params"]["picture"]}
        assert second["inputs"]["picture"][0]["filename"] == "dot.png"
        out = local.get(f"/api/v1/jobs/{second['id']}/outputs/out").content
        assert out == image.read_bytes()
        jobs_dir = app_config.parent / "data" / "jobs"
        stored = jobs_dir / second["id"] / second["inputs"]["picture"][0]["file"]
        assert stored.stat().st_nlink == 2  # hard-linked from the first job, not copied

        # the old job stays in the history, and can be retried again; a finished one cannot
        assert local.get(f"/api/v1/jobs/{first['id']}").json()["state"] == "error"
        assert local.post(f"/api/v1/jobs/{second['id']}/retry").status_code == 409
        assert local.post("/api/v1/jobs/nope/retry").status_code == 404

        # over MCP
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "retry_job", "arguments": {"job_id": first["id"]}}}
        r = local.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json=rpc)
        res = json.loads(r.json()["result"]["content"][0]["text"])
        assert res["ok"] and res["retry_of"] == first["id"] and res["params"]["seed"] == seed
        assert _wait(local, res["job"]["id"])["state"] == "done"

        # inputs removed (as retention does): no longer retryable
        shutil.rmtree(jobs_dir / first["id"])
        assert local.post(f"/api/v1/jobs/{first['id']}/retry").status_code == 410
