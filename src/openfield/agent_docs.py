"""What consumer agents get: the connection details and a SKILL.md built from the live service configs."""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from .config import Config, ServiceSpec
from .params import schema

KEY_ENV = "OPENFIELD_KEY"
CATEGORY_TASKS = {"image": "image generation or editing", "video": "video generation",
                  "video-post": "video upscaling or frame interpolation", "llm": "local LLM calls",
                  "utility": "media inspection"}
ORDER = list(CATEGORY_TASKS)

Availability = Callable[[ServiceSpec], tuple[bool, Optional[str]]]


def proxy_base(base: str, spec: ServiceSpec) -> str:
    tail = (spec.proxy.client_base if spec.proxy else "").strip("/")
    return f"{base}/proxy/{spec.id}" + (f"/{tail}" if tail else "")


def connect_info(config: Config, port: int, availability: Availability) -> dict[str, Any]:
    base = config.public_url(port)
    proxies = [{"id": s.id, "name": s.name, "base_url": proxy_base(base, s), "available": availability(s)[0]}
               for s in config.services.values() if s.kind == "http_proxy"]
    return {"base_url": base, "local_url": f"http://localhost:{port}", "mcp_url": f"{base}/mcp",
            "openapi_url": f"{base}/api/openapi.json", "docs_url": f"{base}/api/docs",
            "skill_url": f"{base}/api/v1/skill", "key_env": KEY_ENV, "proxies": proxies}


def _num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:g}" if isinstance(v, float) else str(v)


def param_line(name: str, p: dict[str, Any]) -> str:
    facts: list[str] = []
    if p["type"] == "enum":
        facts.append("one of " + ", ".join(f"`{v}`" for v in p.get("values") or []))
    elif p["type"] in ("file", "files"):
        kinds = "/".join(a for a in p.get("accept", []) if a != "any") or "any"
        facts.append(f"{kinds} file" + (f", up to {p['max_files']}" if p["type"] == "files" else ""))
    else:
        facts.append(p["type"])
    if p.get("min") is not None or p.get("max") is not None:
        facts.append(f"{_num(p.get('min', ''))}–{_num(p.get('max', ''))}")
    if p.get("multiple_of"):
        facts.append(f"multiple of {p['multiple_of']}")
    if p.get("default") == "random":
        facts.append("random if omitted")
    elif p.get("default") is not None:
        facts.append(f"default {json.dumps(p['default'])}")
    if p.get("required"):
        facts.append("**required**")
    desc = f": {p['description']}" if p.get("description") else ""
    return f"  - `{name}` ({', '.join(facts)}){desc}"


def render_skill(config: Config, port: int, availability: Availability, machine: Optional[str] = None) -> str:
    base = config.public_url(port)
    rank = {c: i for i, c in enumerate(ORDER)}
    specs = sorted((s for s in config.services.values() if s.enabled),
                   key=lambda s: (rank.get(s.category, len(rank)), s.id))
    available = [s for s in specs if availability(s)[0]]
    unavailable = [(s, availability(s)[1]) for s in specs if not availability(s)[0]]
    jobs = [s for s in available if s.kind != "http_proxy"]
    proxies = [s for s in available if s.kind == "http_proxy"]
    tasks = []
    for s in available:
        task = CATEGORY_TASKS.get(s.category, s.category)
        if task not in tasks:
            tasks.append(task)
    models = []
    for s in available:
        if s.model and s.model.split(" ")[0] not in models:
            models.append(s.model.split(" ")[0])
    models_txt = f" ({', '.join(models[:6])})" if models else ""
    description = (f"Run AI media jobs and local LLMs on the openField server at {base}{models_txt}. Use it "
                   f"whenever a task needs {', '.join(tasks) or 'one of these services'}.")
    if len(description) > 1000:
        description = description[:997] + "..."
    H = f'-H "X-Token: ${KEY_ENV}"'
    gpu = config.daemon.resources.get("gpu")
    example = next((s for s in jobs if "prompt" in s.params), jobs[0] if jobs else None)
    example_params = {"prompt": "a lighthouse at dusk, photo"} if example and "prompt" in example.params else {}
    file_example = next(((s, n) for s in jobs for n, p in s.params.items() if p.type in ("file", "files")), None)

    out: list[str] = [
        "---", "name: openfield", f"description: {json.dumps(description, ensure_ascii=False)}", "---", "",
        "# openField", "",
        f"openField runs AI media services and local LLMs on a machine on the local network"
        f"{f' ({machine})' if machine else ''}, at **`{base}`**. Jobs run on its GPU, which takes a limited "
        f"number of jobs at once (slots, set by the owner{f'; {gpu.slots} now' if gpu else ''}), and every job "
        "also needs enough free memory for its model. A request that does not fit is **rejected, not "
        "queued**: wait and retry.", "",
        "## Access", "",
        f"- Send `X-Token: ${KEY_ENV}` (or `Authorization: Bearer ${KEY_ENV}`) on every call. The key is in the "
        f"`{KEY_ENV}` environment variable: never print it, log it or write it into files.",
        f"- An agent running on the openField machine itself can use `http://localhost:{port}` with no key.",
        f"- Two ways in: the REST API below, or the MCP server at `{base}/mcp` (streamable HTTP, same header). "
        "If openField's MCP tools are available to you, prefer them: same operations.",
        f"- Live service list with full parameter schemas: `GET {base}/api/v1/services`. OpenAPI: "
        f"`{base}/api/openapi.json`. Check it when a service below is missing or a parameter is rejected.", "",
        "## Services", "",
        "| id | name | what |", "|---|---|---|",
    ]
    for s in available:
        what = " ".join(s.description.split())
        out.append(f"| `{s.id}` | {s.name} | {what} |")
    if unavailable:
        out += ["", "Configured but unavailable now: " + "; ".join(f"`{s.id}` ({why})" for s, why in unavailable) + "."]
    out += ["", "### Parameters", ""]
    for s in jobs:
        out.append(f"- **`{s.id}`**" + (f" → outputs: {', '.join(f'`{o.name}` ({o.media_type})' for o in s.outputs)}"
                                         if s.outputs else ""))
        out += [param_line(n, p) for n, p in schema(s).items()]
        out += [f"  - Note: {note}" for note in s.agent_notes]
    if proxies:
        out += ["", "## Proxies", ""]
        for s in proxies:
            out.append(f"- **`{s.id}`** ({s.name}): base URL `{proxy_base(base, s)}`, API key `${KEY_ENV}` (an "
                       "OpenAI-style client sends it as `Authorization: Bearer`).")
            out += [f"  - Note: {note}" for note in s.agent_notes]
        out += ["", "Proxy calls take a GPU slot while they are in flight (calls of the same proxy share one). "
                "When every slot is held by a job, they answer 409 too."]
    out += ["", "## REST flow", "", "```sh", f"B={base}",
            "# 1. Is there a free slot? (busy = none free; resources.gpu.used/slots; running jobs with progress, eta_s)",
            f"curl -s {H} $B/api/v1/status"]
    if example:
        body = json.dumps({"service": example.id, "params": example_params}, ensure_ascii=False)
        out += ["# 2. Submit. JSON when there are no files:",
                f"curl -s {H} -H 'Content-Type: application/json' \\", f"  -d '{body}' $B/api/v1/jobs"]
    if file_example:
        s, param = file_example
        job = json.dumps({"service": s.id, "params": {"prompt": "make it night"} if "prompt" in s.params else {}},
                         ensure_ascii=False)
        out += ["#    small files can go as multipart: a 'job' field with the same JSON, plus one field per file "
                "parameter", f"curl -s {H} -F 'job={job}' -F {param}=@input.png $B/api/v1/jobs",
                "#    large files (videos): see \"Large files\" below"]
    out += ["# 3. Poll until state is done / error / cancelled", f"curl -s {H} $B/api/v1/jobs/<id>",
            "# 4. Download each output listed in \"outputs\" (full URLs in \"output_urls\")",
            f"curl -s {H} -o out.png $B/api/v1/jobs/<id>/outputs/<name>", "```", "",
            "Answers to `POST /api/v1/jobs`:",
            "- **202**: `{\"id\", \"state\": \"running\", \"estimate_s\", \"params\"}`; `params` includes the seed used.",
            "- **409 busy**: no free slot, or the job's memory would not fit next to the running jobs. `detail.reason`, "
            "`detail.held_by`, `detail.running` (progress, `eta_s`) and a `Retry-After` header: wait that long (or "
            "poll `/api/v1/status` until a slot is free), then resubmit. Do not hammer it.",
            "- **503**: the job would not fit in memory even with the GPU idle (`detail.available_gb` / `needed_gb`), "
            "or the service is unavailable.",
            "- **400**: invalid parameters (the message names the parameter); **401**: missing or wrong key; "
            "**404**: unknown service.", "",
            "## Large files", "",
            "Never base64 a large file into a request or a tool call. Upload it first, then pass the upload id:",
            "", "```sh",
            f"ID=$(curl -s {H} -H 'Content-Type: application/json' -d '{{\"filename\": \"clip.mp4\"}}' "
            "$B/api/v1/uploads | jq -r .id)",
            f"curl -fsS -T clip.mp4 {H} $B/api/v1/uploads/$ID      # streams to disk; answer has state and sha256",
            "# interrupted? GET $B/api/v1/uploads/$ID gives \"received\"; continue from there:",
            f"curl -fsS -C <received> -T clip.mp4 {H} $B/api/v1/uploads/$ID",
            f"curl -s {H} -H 'Content-Type: application/json' \\",
            "  -d '{\"service\": \"<service>\", \"params\": {}, \"uploads\": {\"<file param>\": \"'$ID'\"}}' $B/api/v1/jobs",
            "```", "",
            f"- An upload stays usable for {_num(config.daemon.uploads.ttl_hours)} h after its last use: after a 409, "
            "resubmit the job with the same id instead of uploading again; several jobs can use one upload. Up to "
            f"{_num(config.daemon.uploads.max_gb)} GB per file (the service's own limit applies too). Optional: send "
            "`\"size\"` and `\"sha256\"` when creating it to have the file checked.",
            f"- Large outputs: download with `curl -fL -C - {H} -o out.mp4 <output url>` (resumable; Range requests "
            "work) and compare with the job's `outputs[].sha256`.",
            "- Over MCP: `create_upload` returns the URL and the curl command to run from your shell; then "
            "`submit_job(..., uploads={\"<param>\": \"<id>\"})`. `get_output` returns small images inline and "
            "anything large as a URL with its SHA-256 and a resumable curl command.", "",
            f"Other calls: `GET /api/v1/jobs?state=&service=&limit=` (history, newest first; files kept "
            f"{_num(round(config.daemon.retention.outputs_hours / 24, 1))} days), `POST /api/v1/jobs/<id>/cancel`, "
            "`DELETE /api/v1/jobs/<id>`.", "",
            "## MCP tools", "",
            "`status`, `list_services`, `create_upload` / `get_upload` / `delete_upload` (large files), "
            "`submit_job(service, params, uploads={param: id}, files=[{param, filename, data_base64}] for small "
            "files only)`, `get_job`, `wait_job(job_id, timeout_s≤120)` (call again while running), `list_jobs`, "
            "`cancel_job`, `get_output` (small images inline; large files as a resumable download URL).", "",
            "## Tips", "",
            "- Slow jobs (video): submit, then poll every 30-60 s (or call `wait_job` repeatedly).",
            "- Keep the returned `seed` to regenerate the same result with other settings.",
            "- Several slots split the GPU: parallel jobs each run slower.", ""]
    return "\n".join(out)
