"""Job lifecycle: admission (locks, external GPU use, memory, disk), execution, status, retention."""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, UploadFile

from .adapters import builtin, http_job, shell
from .adapters.base import JobContext
from .config import Config, ServiceSpec, expand
from .db import DB
from .expr import ExprError, evaluate
from .media import available_memory_gb, classify, find_processes
from .params import ParamError, resolve
from .resources import Locks
from .uploads import StagedFile, Uploads
from .runner import Cancelled

log = logging.getLogger("openfield.jobs")
RUNNERS = {"shell": shell.run, "http_job": http_job.run, "builtin": builtin.run}
ACTIVE = ("queued", "running")


class Busy(HTTPException):
    def __init__(self, detail: dict, retry_after: int):
        super().__init__(409, detail, headers={"Retry-After": str(max(5, retry_after))})


class JobManager:
    def __init__(self, config: Config, db: DB):
        self.config = config
        self.db = db
        self.locks = Locks(capacity=self.slots)
        self.live: dict[str, JobContext] = {}
        self.admission = threading.Lock()
        self.jobs_dir = config.data_dir / "jobs"
        self.uploads = Uploads(config)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()

    # ------------------------------------------------------------ lifecycle

    def startup(self) -> None:
        for job in self.db.jobs_in_states(ACTIVE):
            job.update(state="error", error="interrupted: the daemon restarted while it ran",
                       finished_at=job.get("finished_at") or time.time(), model_state="released")
            self.db.save_job(job)
            for name in ("f", "u", "c", "r"):
                shutil.rmtree(self.jobs_dir / job["id"] / name, ignore_errors=True)
        threading.Thread(target=self._retention, name="retention", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        for ctx in list(self.live.values()):
            ctx.cancel.set()
        for ctx in list(self.live.values()):
            if ctx.thread:
                ctx.thread.join(20)

    # ------------------------------------------------------------ parallel slots (a user setting)

    def slots(self, resource: str) -> int:
        spec = self.config.daemon.resources.get(resource)
        if not spec:
            return 1
        value = self.db.get_setting(f"slots.{resource}", spec.slots)
        return max(1, min(int(value), spec.max_slots))

    def settings(self) -> dict[str, Any]:
        return {"resources": {name: {"slots": self.slots(name), "default": r.slots, "max": r.max_slots,
                                     "memory_margin_gb": r.memory_margin_gb, "description": r.description}
                              for name, r in self.config.daemon.resources.items()}}

    def set_slots(self, resource: str, value: int, who: str) -> None:
        spec = self.config.daemon.resources.get(resource)
        if not spec:
            raise HTTPException(404, f"unknown resource {resource!r}")
        if not 1 <= value <= spec.max_slots:
            raise HTTPException(400, f"{resource} slots must be between 1 and {spec.max_slots}")
        self.db.set_setting(f"slots.{resource}", value, who)
        log.info("%s slots set to %d by %s", resource, value, who)

    # ------------------------------------------------------------ availability and admission

    def availability(self, spec: ServiceSpec) -> tuple[bool, Optional[str]]:
        if not spec.enabled:
            return False, "disabled in its config"
        for pattern in spec.requires:
            matches = glob.glob(str(expand(pattern)))
            if not matches or not all(os.path.exists(m) for m in matches):
                return False, f"missing: {pattern}"
        return True, None

    def _running_on(self, resources: list[str]) -> list[dict]:
        return [self.brief(c.job) for c in self.live.values() if set(c.spec.resources) & set(resources)]

    def _busy(self, reason: str, resource: Optional[str], services: list[str]) -> Busy:
        running = self._running_on([resource] if resource else [])
        etas = [r["eta_s"] for r in running if r["eta_s"] is not None]
        detail = {"error": "busy", "reason": reason, "resource": resource,
                  "held_by": services[0] if services else None, "held_by_all": services,
                  "running": running[0] if running else None, "running_all": running}
        return Busy(detail, int(min(etas)) if etas else 30)

    def _external(self, spec: ServiceSpec) -> dict[str, list[str]]:
        """Generation tools started outside the daemon, per resource of the service."""
        mine = {c.proc.pid for c in self.live.values() if c.proc}
        out = {}
        for r in spec.resources:
            names = self.config.daemon.resources[r].detect_external
            out[r] = find_processes(names, mine) if names else []
        return out

    def _acquire(self, spec: ServiceSpec, owner: str) -> None:
        ext = self._external(spec)
        conflict = self.locks.try_acquire(spec, owner, {r: len(v) for r, v in ext.items()})
        if conflict:
            reason = conflict["reason"]
            outside = sorted({n for v in ext.values() for n in v})
            if conflict["external"] and outside:
                reason += f" — outside the daemon: {', '.join(outside)}"
            raise self._busy(reason, conflict["resource"], conflict["services"])

    def admit_call(self, spec: ServiceSpec, owner: str) -> None:
        """Admission for a proxied call: a slot, no memory check (the upstream manages its own models)."""
        with self.admission:
            self._acquire(spec, owner)

    def reserved_loading_gb(self, resources: list[str]) -> float:
        """Memory promised to running jobs whose model is not loaded yet (free memory does not show it)."""
        return round(sum(c.reserved_gb for c in self.live.values()
                         if set(c.spec.resources) & set(resources)
                         and c.job.get("model_state") in (None, "loading", "per stage")), 1)

    def _admit_job(self, spec: ServiceSpec, owner: str, params: dict) -> float:
        """Take a slot, then check free memory and disk. Returns the memory reserved for the job."""
        self._acquire(spec, owner)
        try:
            need = 0.0
            checked = [r for r in spec.resources if self.config.daemon.resources[r].memory_check]
            if spec.memory_gb is not None and checked:
                need = float(evaluate(spec.memory_gb, params))
                margin = max(self.config.daemon.resources[r].memory_margin_gb for r in checked)
                avail = available_memory_gb()
                pending = self.reserved_loading_gb(checked)
                usable = avail - pending
                if usable < need + margin:
                    others = self._running_on(checked)
                    facts = {"needed_gb": round(need + margin, 1), "available_gb": avail, "reserved_by_loading_jobs_gb": pending}
                    if others:
                        # Other jobs hold memory: it frees up when they finish, so this is a retryable busy.
                        busy = self._busy(f"not enough free memory next to the {len(others)} running job(s): "
                                          f"{need + margin:.0f} GB needed, {usable:.0f} GB usable", checked[0],
                                          [o["service"] for o in others])
                        busy.detail.update(facts)
                        raise busy
                    raise HTTPException(503, {"error": "insufficient_memory", **facts,
                                              "hint": "another app (oMLX, Unsloth Studio...) is holding memory"})
            if shutil.disk_usage(self.jobs_dir).free < self.config.daemon.min_free_disk_gb * 1024**3:
                raise HTTPException(507, f"less than {self.config.daemon.min_free_disk_gb:g} GB free on the Mac")
            return need
        except BaseException:
            self.locks.release(spec, owner)
            raise

    # ------------------------------------------------------------ submit

    def submit(self, spec: ServiceSpec, raw: dict[str, Any], uploads: dict[str, list[UploadFile]],
               client: str) -> dict[str, Any]:
        if spec.kind == "http_proxy":
            raise HTTPException(400, f"{spec.id} is a proxy: call /proxy/{spec.id}/... instead of submitting a job")
        ok, why = self.availability(spec)
        if not ok:
            raise HTTPException(503, f"{spec.id} is not available: {why}")
        try:
            params = resolve(spec, raw, {k: len(v) for k, v in uploads.items() if v})
        except ParamError as e:
            raise HTTPException(400, str(e)) from None
        jid = uuid.uuid4().hex[:16]
        work = self.jobs_dir / jid
        with self.admission:
            reserved = self._admit_job(spec, jid, params)
            try:
                work.mkdir(parents=True)
                inputs = self._save_uploads(spec, uploads, work, params)
                estimate = self._estimate(spec, params)
            except BaseException:
                self.locks.release(spec, jid)
                shutil.rmtree(work, ignore_errors=True)
                raise
            now = time.time()
            job = {"id": jid, "service": spec.id, "state": "running", "params": self._display_params(spec, params),
                   "inputs": inputs, "outputs": [], "result": None, "progress": 0.0, "stage": "starting",
                   "model": spec.model, "model_state": None, "error": None, "log_tail": "", "client": client,
                   "estimate_s": estimate, "created_at": now, "started_at": now, "finished_at": None,
                   "elapsed_s": None, "outputs_purged": False}
            ctx = JobContext(self, job, spec, params, work)
            ctx.reserved_gb = reserved
            self.live[jid] = ctx
            self.db.save_job(job)
            ctx.thread = threading.Thread(target=self._run, args=(ctx,), name=f"job-{jid}", daemon=True)
            ctx.thread.start()
        log.info("job %s accepted: %s for %s (estimate %s s)", jid, spec.id, client, estimate)
        return job

    def _save_uploads(self, spec: ServiceSpec, uploads: dict[str, list[UploadFile]], work: Path,
                      params: dict) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        folder = work / "inputs"
        for name, files in uploads.items():
            files = [f for f in files if f is not None]
            if not files:
                continue
            p = spec.params[name]
            folder.mkdir(exist_ok=True)
            saved, infos = [], []
            for i, up in enumerate(files):
                suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(up.filename or "").suffix.lower())[:8]
                dest = folder / f"{name}_{i + 1:02d}{suffix}"
                written, limit = 0, int(p.max_mb * 1024**2)
                if isinstance(up, StagedFile):  # sent before the job: a hard link, not a copy
                    if up.size > limit:
                        raise HTTPException(400, f"{name}: {up.filename} is larger than {p.max_mb:g} MB")
                    try:
                        os.link(up.path, dest)
                    except OSError:
                        shutil.copyfile(up.path, dest)
                else:
                    with open(dest, "wb") as fh:
                        while chunk := up.file.read(8 << 20):
                            written += len(chunk)
                            if written > limit:
                                raise HTTPException(400, f"{name}: {up.filename} is larger than {p.max_mb:g} MB")
                            fh.write(chunk)
                try:
                    info = classify(dest)
                except ValueError as e:
                    raise HTTPException(400, f"{name}: {e}") from None
                if "any" not in p.accept and info["kind"] not in p.accept:
                    raise HTTPException(400, f"{name}: {up.filename} is {info['kind']}, expected {' or '.join(p.accept)}")
                saved.append(str(dest))
                infos.append({"file": str(dest.relative_to(work)), "filename": up.filename, **info})
            params[name] = saved if p.type == "files" else saved[0]
            inputs[name] = infos
        return inputs

    @staticmethod
    def _display_params(spec: ServiceSpec, params: dict) -> dict:
        """Params for storage and display: file params as input file names, not absolute paths."""
        out = {}
        for k, v in params.items():
            p = spec.params.get(k)
            if p and p.type in ("file", "files") and v:
                out[k] = [Path(x).name for x in v] if isinstance(v, list) else Path(v).name
            else:
                out[k] = v
        return out

    def _estimate(self, spec: ServiceSpec, params: dict) -> Optional[float]:
        try:
            if spec.estimate_s is not None:
                return round(float(evaluate(spec.estimate_s, params)), 1)
            if spec.kind == "builtin" and spec.handler:
                est = builtin.estimate(spec.handler, spec.handler_args, params)
                return round(est, 1) if est is not None else None
        except (ExprError, TypeError, ValueError, ZeroDivisionError) as e:
            log.warning("estimate for %s failed: %s", spec.id, e)
        return None

    # ------------------------------------------------------------ run

    def _run(self, ctx: JobContext) -> None:
        job, spec = ctx.job, ctx.spec
        state, error = "done", None
        try:
            RUNNERS[spec.kind](ctx)
            if ctx.cancel.is_set():
                state = "cancelled"
        except Cancelled:
            state = "cancelled"
        except Exception as e:  # noqa: BLE001 - every failure is reported to the client
            state, error = "error", str(e) or type(e).__name__
            log.exception("job %s (%s) failed", job["id"], spec.id)
        finally:
            self.locks.release(spec, job["id"])
            self.live.pop(job["id"], None)
        for name in ("f", "u", "c", "r"):
            shutil.rmtree(ctx.work / name, ignore_errors=True)
        now = time.time()
        if job.get("model_state") in ("loading", "loaded"):
            job["model_state"] = "released"
        job.update(state=state, error=error if state == "error" else ("cancelled by request" if state == "cancelled" else None),
                   finished_at=now, elapsed_s=round(now - job["started_at"], 1), log_tail="\n".join(ctx.tail))
        if state == "done":
            job["progress"], job["stage"] = 1.0, "done"
        self.db.save_job(job)
        log.info("job %s %s in %.1f s", job["id"], state, job["elapsed_s"])

    # ------------------------------------------------------------ queries and control

    def get(self, jid: str) -> Optional[dict[str, Any]]:
        ctx = self.live.get(jid)
        return ctx.job if ctx else self.db.get_job(jid)

    def remaining_s(self, job: dict) -> Optional[float]:
        if job["state"] not in ACTIVE:
            return None
        p = job.get("progress") or 0.0
        if p > 0.05 and job.get("started_at"):
            return (time.time() - job["started_at"]) * (1 - p) / p
        return (job.get("estimate_s") or 0.0) * (1 - p) if job.get("estimate_s") else None

    def brief(self, job: dict) -> dict[str, Any]:
        eta = self.remaining_s(job)
        prompt = (job.get("params") or {}).get("prompt")
        return {"id": job["id"], "service": job["service"], "state": job["state"], "stage": job.get("stage"),
                "progress": job.get("progress"), "eta_s": round(eta) if eta is not None else None,
                "estimate_s": job.get("estimate_s"), "model": job.get("model"), "model_state": job.get("model_state"),
                "client": job.get("client"), "created_at": job["created_at"], "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"), "elapsed_s": job.get("elapsed_s"), "error": job.get("error"),
                "prompt": prompt[:200] if isinstance(prompt, str) else None,
                "outputs": [{k: o.get(k) for k in ("name", "media_type", "width", "height", "duration_s", "size_bytes")}
                            for o in job.get("outputs") or []],
                "outputs_purged": job.get("outputs_purged", False)}

    def public(self, job: dict) -> dict[str, Any]:
        out = self.brief(job)
        ctx = self.live.get(job["id"])
        out.update(params=job.get("params"), inputs=job.get("inputs"), result=job.get("result"),
                   outputs=job.get("outputs") or [],
                   log_tail="\n".join(ctx.tail) if ctx else job.get("log_tail"))
        return out

    def cancel(self, jid: str) -> bool:
        ctx = self.live.get(jid)
        if not ctx:
            return False
        ctx.cancel.set()
        return True

    def delete(self, jid: str) -> bool:
        if self.cancel(jid):
            ctx = self.live.get(jid)
            if ctx and ctx.thread:
                ctx.thread.join(30)
        if not self.db.get_job(jid):
            return False
        shutil.rmtree(self.jobs_dir / jid, ignore_errors=True)
        self.db.delete_job(jid)
        return True

    def output_path(self, job: dict, name: str) -> Optional[tuple[Path, str]]:
        for o in job.get("outputs") or []:
            if o["name"] == name:
                path = self.jobs_dir / job["id"] / o["file"]
                return (path, o.get("media_type") or "application/octet-stream") if path.exists() else None
        return None

    def input_path(self, job: dict, param: str, index: int = 0) -> Optional[Path]:
        items = (job.get("inputs") or {}).get(param) or []
        if index < len(items):
            path = self.jobs_dir / job["id"] / items[index]["file"]
            return path if path.exists() else None
        return None

    def status(self) -> dict[str, Any]:
        """busy: some resource has no free slot (a new job on it would get 409). active: anything running."""
        running = [self.brief(c.job) for c in self.live.values()]
        names = list(self.config.daemon.resources)
        resources = self.locks.snapshot(names)
        mine = {c.proc.pid for c in self.live.values() if c.proc}
        outside: list[str] = []
        for name, r in self.config.daemon.resources.items():
            ext = find_processes(r.detect_external, mine) if r.detect_external else []
            resources[name].update(external=ext, used=resources[name]["used"] + len(ext),
                                   reserved_loading_gb=self.reserved_loading_gb([name]))
            resources[name]["free"] = max(0, resources[name]["slots"] - resources[name]["used"])
            outside += ext
        finished, _ = self.db.list_jobs(limit=1, state="done")
        return {"busy": any(r["free"] == 0 for r in resources.values()),
                "active": bool(running) or any(r["used"] for r in resources.values()),
                "resources": resources, "running": running,
                "external_gpu_process": outside[0] if outside else None,
                "last_finished": self.brief(finished[0]) if finished else None,
                "memory_available_gb": available_memory_gb(), "counts": self.db.stats()}

    # ------------------------------------------------------------ retention

    def _retention(self) -> None:
        while not self._stop.wait(600):
            try:
                ret = self.config.daemon.retention
                now = time.time()
                for job in self.db.finished_before(now - ret.outputs_hours * 3600, purged=False):
                    shutil.rmtree(self.jobs_dir / job["id"], ignore_errors=True)
                    job["outputs_purged"] = True
                    self.db.save_job(job)
                for job in self.db.finished_before(now - ret.records_days * 86400):
                    shutil.rmtree(self.jobs_dir / job["id"], ignore_errors=True)
                    self.db.delete_job(job["id"])
                self.db.purge_requests(now - ret.records_days * 86400)
                if self.uploads.purge_expired():
                    log.info("expired uploads removed")
            except Exception:  # noqa: BLE001
                log.exception("retention pass failed")
