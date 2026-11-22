"""Run a tool as a child process: stream its lines, report progress, honour cancel and timeouts."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .config import ProgressSpec
from .expr import evaluate, render


class Cancelled(Exception):
    pass


POSIX = os.name != "nt"


def kill_group(p: subprocess.Popen, grace: float = 15) -> None:
    """Stop the tool and its children: SIGTERM to the process group, SIGKILL after `grace` seconds
    (Windows: taskkill /T on the process tree)."""
    if not POSIX:
        subprocess.run(["taskkill", "/T", "/PID", str(p.pid)], capture_output=True)
        try:
            p.wait(grace)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        p.wait(grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_process(cmd: list[str], cwd: Path, env: Mapping[str, str], on_line: Callable[[str], None],
                cancel: threading.Event, on_start: Optional[Callable[[subprocess.Popen], None]] = None,
                timeout_s: Optional[float] = None, on_tick: Optional[Callable[[], None]] = None) -> int:
    group = {"start_new_session": True} if POSIX else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    p = subprocess.Popen(cmd, cwd=str(cwd), env=dict(env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, text=True, errors="replace", **group)
    if on_start:
        on_start(p)

    def reader() -> None:
        for line in p.stdout:  # type: ignore[union-attr]  # universal newlines split \r progress bars too
            line = line.rstrip()
            if line:
                on_line(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    started = time.time()
    while p.poll() is None:
        time.sleep(0.3)
        if cancel.is_set():
            kill_group(p)
            raise Cancelled()
        if timeout_s and time.time() - started > timeout_s:
            kill_group(p, grace=5)
            raise TimeoutError(f"{Path(cmd[0]).name} took longer than {timeout_s:g} s")
        if on_tick:
            on_tick()
    t.join(5)
    return p.returncode


class StepProgress:
    """Turns a tool's output into a fraction of its step (see ProgressSpec)."""

    def __init__(self, spec: ProgressSpec, names: Mapping[str, Any]):
        self.spec = spec
        self.names = names
        self.started = spec.start_after is None
        self.start_re = re.compile(spec.start_after) if spec.start_after else None
        self.regex = re.compile(spec.regex) if spec.regex else None
        self.phases = [(re.compile(ph.regex), ph) for ph in spec.phases]
        self.total_eq = evaluate(spec.total_equals, names) if spec.total_equals is not None else None
        self.total = evaluate(spec.total, names) if spec.total is not None else None
        self.watch = Path(render(spec.watch_dir, names)) if spec.watch_dir else None

    def _scale(self, frac: float) -> float:
        frac = max(0.0, min(1.0, frac))
        return self.spec.scale_from + (self.spec.scale_to - self.spec.scale_from) * frac

    def feed(self, line: str) -> Optional[tuple[float, Optional[str]]]:
        for rx, ph in self.phases:
            if rx.search(line):
                return ph.progress, ph.label
        kind = self.spec.kind
        if kind == "regex" and self.regex:
            if not self.started:
                if self.start_re and self.start_re.search(line):
                    self.started = True
                return None
            m = self.regex.search(line)
            if not m:
                return None
            done, total = int(m.group("done")), int(m.group("total"))
            if total <= 0:
                return None
            if self.total_eq is not None:
                allowed = self.total_eq if isinstance(self.total_eq, list) else [self.total_eq]
                if total not in {int(a) for a in allowed if a is not None}:
                    return None
            label = render(self.spec.label, {**self.names, "done": done, "total": total}) if self.spec.label else None
            return self._scale(done / total), label
        if kind == "ffmpeg" and line.startswith("frame=") and self.total:
            try:
                return self._scale(int(line[6:]) / float(self.total)), None
            except ValueError:
                return None
        return None

    def poll(self) -> Optional[float]:
        if self.spec.kind == "files" and self.watch and self.total:
            try:
                n = sum(1 for _ in os.scandir(self.watch))
            except OSError:
                return None
            return self._scale(n / float(self.total))
        return None
