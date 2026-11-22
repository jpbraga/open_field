"""http_job adapter: drive an asynchronous HTTP API (submit, poll until done, fetch the files).

Templates can use the job's params, `submit` (the submit response JSON), `r` (the last poll response),
`secret.NAME` (from daemon.yaml secrets) and `b64.PARAM` (an uploaded file as a data: URL).
"""
from __future__ import annotations

import base64
import mimetypes
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from ..auth import read_secret
from ..config import HttpCall
from ..expr import evaluate, render, render_str
from ..runner import Cancelled
from .base import JobContext


class Lazy(Mapping):
    """A mapping whose values are computed on first access (secrets, data URLs)."""

    def __init__(self, keys: list[str], compute: Callable[[str], Any]):
        self._keys, self._compute, self._cache = keys, compute, {}

    def __getitem__(self, key: str) -> Any:
        if key not in self._keys:
            raise KeyError(key)
        if key not in self._cache:
            self._cache[key] = self._compute(key)
        return self._cache[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


def data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()


def names_for(ctx: JobContext) -> dict[str, Any]:
    secrets_cfg = ctx.manager.config.daemon.secrets
    files = {k: v for k, v in ctx.params.items() if k in ctx.spec.params and ctx.spec.params[k].type in ("file", "files") and v}

    def b64(key: str) -> Any:
        value = files[key]
        return [data_url(p) for p in value] if isinstance(value, list) else data_url(value)

    return {**ctx.names(), "secret": Lazy(list(secrets_cfg), lambda k: read_secret(secrets_cfg[k])),
            "b64": Lazy(list(files), b64)}


def call(client: httpx.Client, spec: HttpCall, names: Mapping[str, Any]) -> Any:
    headers = {k: render_str(v, names) for k, v in spec.headers.items()}
    body = render(spec.json_body, names) if spec.json_body is not None else None
    resp = client.request(spec.method, render_str(spec.url, names), headers=headers, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"{spec.method} {resp.request.url.path} answered {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        return {"text": resp.text}


def run(ctx: JobContext) -> None:
    http = ctx.spec.http
    assert http is not None
    names = names_for(ctx)
    deadline = time.time() + http.timeout_s
    with httpx.Client(timeout=120) as client:
        ctx.set_stage("submitting")
        names["submit"] = call(client, http.submit, names)
        if http.poll:
            ctx.set_stage("waiting for the upstream job")
            while True:
                if ctx.cancel.is_set():
                    if http.cancel:
                        try:
                            call(client, http.cancel, names)
                        except (httpx.HTTPError, RuntimeError):
                            pass
                    raise Cancelled()
                if time.time() > deadline:
                    raise TimeoutError(f"the upstream job took longer than {http.timeout_s:g} s")
                names["r"] = call(client, http.poll, names)
                if http.poll.error:
                    err = evaluate(http.poll.error, names)
                    if err:
                        raise RuntimeError(f"upstream error: {err}")
                if http.poll.progress:
                    frac = evaluate(http.poll.progress, names)
                    if isinstance(frac, (int, float)):
                        ctx.set_progress(0.95 * float(frac))
                if http.poll.stage:
                    stage = evaluate(http.poll.stage, names)
                    if stage:
                        ctx.job["stage"] = str(stage)
                if evaluate(http.poll.done, names):
                    break
                ctx.persist()
                time.sleep(http.poll.interval_s)
        for f in http.fetch:
            ctx.set_stage(f"downloading {f.name}")
            url = render_str(f.url, names)
            dest = ctx.work / Path(render_str(f.filename, {**names, "name": f.name})).name
            headers = {k: render_str(v, names) for k, v in f.headers.items()}
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"download of {f.name} answered {resp.status_code}")
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
            ctx.add_output(f.name, dest, f.media_type)
    ctx.job["result"] = {"upstream": {k: v for k, v in (names.get("r") or names["submit"] or {}).items()
                                      if isinstance(v, (str, int, float, bool))}} if isinstance(
        names.get("r") or names["submit"], dict) else None
