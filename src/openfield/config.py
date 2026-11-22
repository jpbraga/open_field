"""Daemon and service configuration, read from YAML.

config/daemon.yaml   network, data folder, auth source, resources (locks), retention, secrets
config/services/*.yaml   one service each: adapter kind, parameters, commands or upstream, outputs
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Expr = Union[str, float, int, None]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- daemon

class ResourceSpec(Strict):
    """A lock that services share, with a number of parallel slots (the user can change it at run time,
    from 1 to max_slots). `reject`: a request that finds no free slot, or not enough free memory, gets 409."""
    policy: Literal["reject"] = "reject"
    description: str = ""
    slots: int = Field(1, ge=1)        # default number of jobs that may hold the resource at once
    max_slots: int = Field(4, ge=1)    # upper bound offered in the settings
    # Executables (or Python entry-point scripts) that, when run by someone else, also take a slot.
    detect_external: list[str] = Field(default_factory=list)
    # Check the Mac's available memory against each service's memory_gb before admitting a job.
    memory_check: bool = True
    memory_margin_gb: float = 4.0      # kept free on top of the job's own memory_gb


class SecretSpec(Strict):
    """A value read at use time from a JSON file (never copied into config or logs)."""
    file: str
    json_path: str


class AuthSpec(Strict):
    # Accepted keys: oMLX's auth.api_key and auth.sub_keys[] (with their names), when that file exists,
    # plus openField's own keys file ({data_dir}/keys.json, managed with `openfield key`).
    omlx_settings: str = "~/.omlx/settings.json"
    # Requests from this machine through a loopback address (localhost, 127.0.0.1, ::1) need no key,
    # unless a browser page from another site sent them.
    loopback_without_key: bool = True
    session_days: int = 7


class RetentionSpec(Strict):
    outputs_hours: float = 168
    records_days: float = 90


class UploadsSpec(Strict):
    ttl_hours: float = 48   # an upload is deleted this long after its last use
    max_gb: float = 200     # largest single upload


class DaemonConfig(Strict):
    host: str = "0.0.0.0"
    port: int = 8765
    public_url: str = "auto"  # base of the output URLs handed to agents; "auto" = http://<LAN IP>:<port>
    data_dir: str = "~/.local/share/openfield"
    min_free_disk_gb: float = 20
    auth: AuthSpec = Field(default_factory=AuthSpec)
    retention: RetentionSpec = Field(default_factory=RetentionSpec)
    uploads: UploadsSpec = Field(default_factory=UploadsSpec)
    resources: dict[str, ResourceSpec] = Field(default_factory=dict)
    secrets: dict[str, SecretSpec] = Field(default_factory=dict)


# ---------------------------------------------------------------- services

class ParamSpec(Strict):
    type: Literal["string", "int", "float", "bool", "enum", "json", "file", "files"]
    description: str = ""
    required: bool = False
    default: Any = None  # "random" for int: a random seed
    min: Optional[float] = None
    max: Optional[float] = None
    multiple_of: Optional[int] = None
    max_length: Optional[int] = None
    values: Optional[list[Any]] = None  # enum
    # file / files
    accept: list[Literal["image", "video", "audio", "any"]] = Field(default_factory=lambda: ["any"])
    max_files: int = 1
    max_mb: float = 2048


class Rule(Strict):
    expr: str
    message: str


class Phase(Strict):
    regex: str
    progress: float
    label: Optional[str] = None


class ProgressSpec(Strict):
    """How a step reports progress.
    regex: a line with named groups `done` and `total` (optionally only after `start_after` matched,
           and only bars whose total equals `total_equals`), mapped into [scale_from, scale_to].
    ffmpeg: ffmpeg `-progress pipe:1` frame counter against `total`.
    files:  files appearing in `watch_dir` against `total`."""
    kind: Literal["none", "regex", "ffmpeg", "files"] = "none"
    regex: Optional[str] = None
    start_after: Optional[str] = None
    total_equals: Expr = None
    total: Expr = None
    watch_dir: Optional[str] = None
    label: Optional[str] = None
    scale_from: float = 0.0
    scale_to: float = 1.0
    phases: list[Phase] = Field(default_factory=list)


class OptionalArgs(Strict):
    when: str
    args: list[str]


class StepSpec(Strict):
    name: str
    when: Optional[str] = None
    command: list[str]
    optional_args: list[OptionalArgs] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    progress: ProgressSpec = Field(default_factory=ProgressSpec)
    weight: Expr = 1.0
    # The step's process holds the service's model: shown as loading -> loaded -> released.
    holds_model: bool = False
    stage: Optional[str] = None  # label shown while the step runs (default: the step name)
    timeout_s: Optional[float] = None


class OutputSpec(Strict):
    name: str
    path: str
    media_type: str = "application/octet-stream"
    when: Optional[str] = None


class ProxySpec(Strict):
    upstream: str
    timeout_s: float = 3600
    # passthrough: forward the client's Authorization (or X-Token as a Bearer); headers: set these instead.
    upstream_auth: Literal["passthrough", "headers", "none"] = "passthrough"
    headers: dict[str, str] = Field(default_factory=dict)  # values may use {secret.NAME}
    # Path clients add to /proxy/<id>/ to get their base URL (e.g. "v1" for an OpenAI-compatible API).
    client_base: str = ""


class HttpCall(Strict):
    method: Literal["GET", "POST", "PUT", "DELETE"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: Any = Field(default=None, alias="json")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HttpPoll(HttpCall):
    method: Literal["GET", "POST", "PUT", "DELETE"] = "GET"
    interval_s: float = 2.0
    done: str = "True"            # expression over `r` (the poll response JSON) and the params
    error: Optional[str] = None   # expression giving an error message (or a falsy value)
    progress: Optional[str] = None
    stage: Optional[str] = None


class HttpFetch(Strict):
    name: str
    url: str
    media_type: str = "application/octet-stream"
    filename: str = "{name}"
    headers: dict[str, str] = Field(default_factory=dict)


class HttpJobSpec(Strict):
    submit: HttpCall
    poll: Optional[HttpPoll] = None
    fetch: list[HttpFetch] = Field(default_factory=list)
    cancel: Optional[HttpCall] = None
    timeout_s: float = 7200


class ServiceSpec(Strict):
    id: str
    name: str
    description: str = ""
    category: str = "other"
    kind: Literal["shell", "http_job", "http_proxy", "builtin"]
    enabled: bool = True
    resources: list[str] = Field(default_factory=list)
    # exclusive: one job holds the resource; shared: up to max_concurrency calls of THIS service share it.
    lock: Literal["exclusive", "shared"] = "exclusive"
    max_concurrency: int = 1
    model: Optional[str] = None
    memory_gb: Expr = None
    estimate_s: Expr = None
    requires: list[str] = Field(default_factory=list)  # paths (globs) that must exist
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    derive: dict[str, str] = Field(default_factory=dict)
    validate_rules: list[Rule] = Field(default_factory=list, alias="validate")
    env: dict[str, str] = Field(default_factory=dict)
    steps: list[StepSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    handler: Optional[str] = None
    handler_args: dict[str, Any] = Field(default_factory=dict)
    proxy: Optional[ProxySpec] = None
    http: Optional[HttpJobSpec] = None
    source: Optional[str] = None  # file the spec came from
    agent_notes: list[str] = Field(default_factory=list)  # tips for agents, in the generated skill

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("id may only contain letters, digits, - and _")
        return v

    def check(self) -> None:
        if self.kind == "shell" and not self.steps:
            raise ValueError("a shell service needs steps")
        if self.kind == "http_proxy" and not self.proxy:
            raise ValueError("an http_proxy service needs proxy")
        if self.kind == "http_job" and not self.http:
            raise ValueError("an http_job service needs http")
        if self.kind == "builtin" and not self.handler:
            raise ValueError("a builtin service needs handler")


# The installed application folder (the repository checkout): `{app}` in config paths.
APP_ROOT = Path(__file__).resolve().parents[2]


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path).replace("{app}", str(APP_ROOT)))))


def lan_ip() -> str:
    """This machine's address on the local network (no packet is sent)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def default_config_dir() -> Path:
    """OPENFIELD_CONFIG, else the user's config folder when it exists, else the app's own config/."""
    if os.environ.get("OPENFIELD_CONFIG"):
        return expand(os.environ["OPENFIELD_CONFIG"])
    user = (Path(os.environ.get("APPDATA", Path.home())) / "openfield") if os.name == "nt" else \
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "openfield"
    return user if (user / "daemon.yaml").exists() else APP_ROOT / "config"


class Config:
    def __init__(self, root: Path):
        self.root = root
        self.daemon = DaemonConfig()
        self.services: dict[str, ServiceSpec] = {}
        self.errors: dict[str, str] = {}

    @property
    def data_dir(self) -> Path:
        return expand(self.daemon.data_dir)

    def public_url(self, port: int | None = None) -> str:
        if self.daemon.public_url and self.daemon.public_url != "auto":
            return self.daemon.public_url.rstrip("/")
        return f"http://{lan_ip()}:{port or self.daemon.port}"

    def load(self) -> "Config":
        daemon_file = self.root / "daemon.yaml"
        raw = yaml.safe_load(daemon_file.read_text()) if daemon_file.exists() else {}
        self.daemon = DaemonConfig(**(raw or {}))
        services: dict[str, ServiceSpec] = {}
        errors: dict[str, str] = {}
        for f in sorted((self.root / "services").glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text()) or {}
                spec = ServiceSpec(**data, source=str(f))
                spec.check()
                for r in spec.resources:
                    if r not in self.daemon.resources:
                        raise ValueError(f"unknown resource {r!r} (declare it in daemon.yaml)")
                if spec.id in services:
                    raise ValueError(f"duplicate service id {spec.id!r}")
                services[spec.id] = spec
            except (ValidationError, ValueError, yaml.YAMLError) as e:
                errors[f.name] = str(e)
        self.services, self.errors = services, errors
        return self
