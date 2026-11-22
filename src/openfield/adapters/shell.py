"""shell adapter: run the service's steps as commands, then collect its outputs."""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Any, Mapping

from ..config import ServiceSpec, StepSpec
from ..expr import evaluate, render, render_str
from .base import JobContext

_LONE = re.compile(r"^\{([A-Za-z_][\w.]*)\}$")


def resolve_path(path: str, pattern: bool) -> str:
    """~ expansion, and when the config template is a glob, its newest match (e.g. a versioned build folder)."""
    path = os.path.expanduser(path)
    if pattern:
        matches = [m for m in glob.glob(path) if os.path.exists(m)]
        if not matches:
            raise RuntimeError(f"nothing matches {path}")
        return max(matches, key=os.path.getmtime)
    return path


def _arg(template: str, names: Mapping[str, Any]) -> list[str]:
    """One template argument -> argv items. A lone placeholder passes the value verbatim (a list expands
    to several items); only the config's own text decides ~ expansion and globbing, never a user value."""
    if _LONE.match(template):
        value = render(template, names)
        if isinstance(value, list):
            return [str(v) for v in value]
        return [render_str(template, names)]
    out = render_str(template, names)
    pattern = template.startswith(("~", "/")) and any(c in template for c in "*?[")
    if template.startswith("~") or pattern:
        return [resolve_path(out, pattern)]
    return [out]


def build_command(step: StepSpec, names: Mapping[str, Any]) -> list[str]:
    cmd: list[str] = []
    for t in step.command:
        cmd += _arg(t, names)
    for opt in step.optional_args:
        if evaluate(opt.when, names):
            for t in opt.args:
                cmd += _arg(t, names)
    return cmd


def environment(spec: ServiceSpec, step: StepSpec, names: Mapping[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({k: render_str(v, names) for k, v in spec.env.items()})
    env.update({k: render_str(v, names) for k, v in step.env.items()})
    return env


def run(ctx: JobContext) -> None:
    spec = ctx.spec
    names = ctx.names()
    active = [s for s in spec.steps if not s.when or evaluate(s.when, names)]
    weights = [max(0.0, float(evaluate(s.weight, names) or 0.0)) for s in active]
    total = sum(weights) or 1.0
    done = 0.0
    for step, w in zip(active, weights):
        if ctx.cancel.is_set():
            break
        ctx.run_tool(build_command(step, names), environment(spec, step, names), stage=step.stage or step.name,
                     base=done / total, weight=w / total, progress=step.progress, holds_model=step.holds_model,
                     timeout_s=step.timeout_s, names=names)
        done += w
    collect_outputs(ctx, names)


def collect_outputs(ctx: JobContext, names: Mapping[str, Any]) -> None:
    for out in ctx.spec.outputs:
        if out.when and not evaluate(out.when, names):
            continue
        ctx.add_output(out.name, Path(os.path.expanduser(render_str(out.path, names))), out.media_type)
