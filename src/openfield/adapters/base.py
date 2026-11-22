"""What an adapter sees of a running job."""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ..config import ProgressSpec, ServiceSpec
from ..media import describe_output
from ..runner import StepProgress, run_process
from ..uploads import file_sha256

if TYPE_CHECKING:
    from ..jobs import JobManager

NOISE = re.compile(r"^\s*\d+(\.\d+)?%\s*$|^\w+=\S*$|\|\s*\d+\s*/\s*\d+|\d+/\d+ \[|it/s|s/it")


class JobContext:
    def __init__(self, manager: "JobManager", job: dict, spec: ServiceSpec, params: dict, work: Path):
        self.manager = manager
        self.job = job
        self.spec = spec
        self.params = params
        self.work = work
        self.cancel = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.tail: deque[str] = deque(maxlen=20)
        self.thread: Optional[threading.Thread] = None
        self.reserved_gb = 0.0  # memory admitted for this job (counted until its model reports loaded)
        self._last_persist = 0.0

    # ------------------------------------------------------------ state

    def names(self) -> dict[str, Any]:
        from ..config import APP_ROOT

        return {**self.params, "work": str(self.work), "home": str(Path.home()), "app": str(APP_ROOT),
                "job_id": self.job["id"], "service": self.spec.id}

    def set_progress(self, frac: float, stage: Optional[str] = None) -> None:
        self.job["progress"] = round(max(self.job.get("progress") or 0.0, min(0.999, frac)), 4)
        if stage:
            self.job["stage"] = stage
        self.persist()

    def set_stage(self, stage: str) -> None:
        self.job["stage"] = stage
        self.persist(force=True)

    def set_model(self, state: str) -> None:
        self.job["model_state"] = state
        self.persist(force=True)

    def log(self, line: str) -> None:
        self.tail.append(line[:400])

    def persist(self, force: bool = False) -> None:
        if force or time.time() - self._last_persist > 2:
            self._last_persist = time.time()
            self.job["log_tail"] = "\n".join(self.tail)
            self.manager.db.save_job(self.job)

    def add_output(self, name: str, path: Path, media_type: str) -> None:
        if not path.exists():
            raise RuntimeError(f"the output {name!r} was not produced ({path.name})")
        info = describe_output(path, media_type)
        info["sha256"] = file_sha256(path)  # lets clients check a large download (resumed with Range requests)
        rel = path.relative_to(self.work) if path.is_relative_to(self.work) else Path(path.name)
        self.job.setdefault("outputs", [])
        self.job["outputs"] = [o for o in self.job["outputs"] if o["name"] != name] + [
            {"name": name, "file": str(rel), **info}]

    # ------------------------------------------------------------ tools

    def run_tool(self, cmd: list[str], env: Mapping[str, str], stage: str, base: float, weight: float,
                 progress: Optional[ProgressSpec] = None, holds_model: bool = False,
                 timeout_s: Optional[float] = None, names: Optional[Mapping[str, Any]] = None,
                 on_line: Optional[Callable[[str], None]] = None) -> None:
        """Run one process as a stage of the job, mapping its progress into [base, base + weight]."""
        tracker = StepProgress(progress, names or self.names()) if progress else None
        self.set_stage(stage)
        if holds_model:
            self.set_model("loading")

        def line_cb(line: str) -> None:
            if on_line:
                on_line(line)
            if tracker:
                got = tracker.feed(line)
                if got:
                    frac, label = got
                    if holds_model and self.job.get("model_state") == "loading":
                        self.set_model("loaded")
                    self.set_progress(base + weight * frac, label)
            if not NOISE.search(line):
                self.log(line)

        def tick() -> None:
            if tracker:
                frac = tracker.poll()
                if frac is not None:
                    self.set_progress(base + weight * frac)
            self.persist()

        def started(p: subprocess.Popen) -> None:
            self.proc = p

        try:
            rc = run_process(cmd, self.work, env, line_cb, self.cancel, on_start=started,
                             timeout_s=timeout_s, on_tick=tick)
        finally:
            self.proc = None
            if holds_model:
                self.set_model("released")
        if rc != 0:
            raise RuntimeError(f"{stage} failed (exit {rc}): " + " | ".join(list(self.tail)[-5:]))
        self.set_progress(base + weight)
