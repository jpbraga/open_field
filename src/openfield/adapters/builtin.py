"""builtin adapter: Python handlers for jobs that need more than a list of commands.

video_post   Real-ESRGAN upscaling and RIFE interpolation (frame-rate doubling or in-between frames
             at cuts), with the source audio copied untouched and the source colours kept.
probe        ffprobe of an uploaded file.
"""
from __future__ import annotations

import os
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from ..config import ProgressSpec, expand
from ..media import FFMPEG, classify, color_plan, probe_video
from .base import JobContext

# load:proc:save threads: the default 1:2:2 leaves the GPU waiting on PNG I/O; 2:4:4 measured ~1.8x faster.
NCNN_THREADS = "2:4:4"
UPSCALE_MODELS = {"realesr-animevideov3": (2, 3, 4), "realesrgan-x4plus": (4,), "realesrgan-x4plus-anime": (4,)}


def _frames(d: Path) -> list[Path]:
    return sorted(d.glob("*.png"))


def video_post_plan(params: dict[str, Any], n_in: int, mode: str) -> list[tuple[str, float]]:
    """Estimated seconds per stage (measured on 720p24 input on the M5 Max)."""
    upscale, interp = "upscale" in mode, "interpolate" in mode
    area = (params.get("scale") or 2) ** 2 if upscale else 1
    cuts = params.get("cuts") or [] if interp else []
    inserted = sum(int(c["insert"]) for c in cuts)
    mult = params.get("fps_multiplier") if interp else None
    n_out = (n_in + inserted) * (mult or 1)
    plan = [("extract", 0.003 * n_in)]
    if upscale:
        plan.append(("upscale", 0.024 * n_in * area))
    if inserted:
        plan.append(("cuts", (0.5 + 0.05 * area) * inserted))
    if mult:
        plan.append(("interpolate", 0.022 * n_out * area))
    plan.append(("encode", 0.0055 * n_out * area))
    return plan


def video_post(ctx: JobContext, mode: str, realesrgan_dir: str, rife_dir: str, rife_model: str = "rife-v4.6") -> None:
    p = ctx.params
    esrgan_dir, rife_root = expand(realesrgan_dir), expand(rife_dir)
    exe = ".exe" if os.name == "nt" else ""
    esrgan, rife, rife_model_dir = (esrgan_dir / f"realesrgan-ncnn-vulkan{exe}", rife_root / f"rife-ncnn-vulkan{exe}",
                                    rife_root / rife_model)
    env = dict(os.environ)
    src = Path(p["video"])
    info = probe_video(src)
    v = info["video"]
    fps = Fraction(v["fps_fraction"]) if v.get("fps_fraction") else Fraction(24)
    n_in = v.get("frames") or 0
    colors = color_plan(info)
    upscale, interp = "upscale" in mode, "interpolate" in mode
    mult = p.get("fps_multiplier") if interp else None
    cuts = sorted(p.get("cuts") or [], key=lambda c: int(c["frame"])) if interp else []
    if interp and not mult and not cuts:
        mult = 2
    if upscale and p["scale"] not in UPSCALE_MODELS.get(p["model"], ()):
        raise ValueError(f"{p['model']} supports scale {UPSCALE_MODELS.get(p['model'])}")
    plan = video_post_plan({**p, "fps_multiplier": mult}, n_in, mode)
    total = sum(w for _, w in plan) or 1.0
    weights = {k: w / total for k, w in plan}
    base = 0.0
    ctx.job["model_state"] = "per stage"

    f = ctx.work / "f"
    f.mkdir(exist_ok=True)
    ctx.run_tool([FFMPEG, "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1", "-i", str(src),
                  "-map", "0:v:0", "-fps_mode", "passthrough",
                  "-vf", f"scale=in_color_matrix={colors['matrix']}:in_range={colors['range']}"
                         ":flags=lanczos+accurate_rnd+full_chroma_int,format=rgb24",
                  "-compression_level", "1", str(f / "%06d.png")],
                 env, "extracting frames", base, weights["extract"], ProgressSpec(kind="ffmpeg", total=max(1, n_in)))
    base += weights["extract"]
    cur = f
    n_in = len(_frames(cur))
    for c in cuts:
        if int(c["frame"]) >= n_in - 1:
            raise ValueError(f"cut at frame {c['frame']} is out of range: the video has {n_in} frames (0..{n_in - 1})")

    if upscale:
        u = ctx.work / "u"
        u.mkdir(exist_ok=True)
        ctx.run_tool([str(esrgan), "-i", str(cur), "-o", str(u), "-n", p["model"], "-s", str(p["scale"]), "-f", "png",
                      "-m", str(esrgan_dir / "models"), "-j", NCNN_THREADS],
                     env, f"upscaling x{p['scale']} ({p['model']})", base, weights["upscale"],
                     ProgressSpec(kind="files", watch_dir=str(u), total=n_in))
        base += weights["upscale"]
        if len(_frames(u)) != n_in:
            raise RuntimeError(f"upscale produced {len(_frames(u))} of {n_in} frames")
        shutil.rmtree(cur)
        cur = u

    if cuts:
        c_dir = ctx.work / "c"
        c_dir.mkdir(exist_ok=True)
        files = _frames(cur)
        inserts = {int(c["frame"]): int(c["insert"]) for c in cuts}
        total_new = sum(inserts.values())
        made, idx = 0, 1
        for i, fp in enumerate(files):
            a = c_dir / f"{idx:06d}.png"
            os.rename(fp, a)
            idx += 1
            for k in range(1, inserts.get(i, 0) + 1):
                out = c_dir / f"{idx:06d}.png"
                ctx.run_tool([str(rife), "-0", str(a), "-1", str(files[i + 1]), "-o", str(out), "-m", str(rife_model_dir),
                              "-s", f"{k / (inserts[i] + 1):.6f}"], env, f"in-between frames after {i} ({k}/{inserts[i]})",
                             base + weights["cuts"] * made / total_new, weights["cuts"] / total_new)
                if not out.exists():
                    raise RuntimeError(f"RIFE did not write the in-between frame {k} after frame {i}")
                made += 1
                idx += 1
        base += weights["cuts"]
        shutil.rmtree(cur)
        cur = c_dir

    n_cur = len(_frames(cur))
    out_fps = fps
    if mult:
        r = ctx.work / "r"
        r.mkdir(exist_ok=True)
        target = n_cur * mult
        ctx.run_tool([str(rife), "-i", str(cur), "-o", str(r), "-m", str(rife_model_dir), "-n", str(target),
                      "-f", "%06d.png", "-j", NCNN_THREADS], env, f"interpolating x{mult}", base, weights["interpolate"],
                     ProgressSpec(kind="files", watch_dir=str(r), total=target))
        base += weights["interpolate"]
        if len(_frames(r)) != target:
            raise RuntimeError(f"interpolation produced {len(_frames(r))} of {target} frames")
        shutil.rmtree(cur)
        cur, out_fps, n_cur = r, fps * mult, target

    out, tmp_out = ctx.work / "out.mp4", ctx.work / "out.partial.mp4"
    size = f"scale=-2:{p['out_height']}:" if p.get("out_height") else "scale="
    vf = (f"{size}flags=lanczos+accurate_rnd+full_chroma_int:out_color_matrix={colors['matrix']}"
          f":out_range={colors['range']},format={p['pix_fmt']},{colors['setparams']}")
    audio = ["-map", "1:a?", "-c:a", "copy"] if info["audio"] and p.get("keep_audio", True) else ["-an"]
    ctx.run_tool([FFMPEG, "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1",
                  "-framerate", str(out_fps), "-pattern_type", "glob", "-i", str(cur / "*.png"), "-i", str(src),
                  "-map", "0:v:0", *audio, "-c:v", "libx264", "-crf", str(p["crf"]), "-preset", "slow", "-vf", vf,
                  "-pix_fmt", p["pix_fmt"], "-movflags", "+faststart", "-f", "mp4", str(tmp_out)],
                 env, "encoding mp4", base, weights["encode"], ProgressSpec(kind="ffmpeg", total=max(1, n_cur)))
    os.replace(tmp_out, out)
    shutil.rmtree(cur, ignore_errors=True)
    ctx.job["model_state"] = "released"
    ctx.add_output("video", out, "video/mp4")
    ctx.job["result"] = {"frames_in": n_in, "frames_out": n_cur, "fps_out": float(out_fps),
                         "inserted_frames": [{"after_frame": int(c["frame"]), "insert": int(c["insert"])} for c in cuts] or None,
                         "note": "cuts are applied on the source frame numbers before the fps multiplier"
                         if cuts and mult else None}


def probe(ctx: JobContext) -> None:
    path = Path(ctx.params["file"])
    kind = classify(path)
    ctx.job["result"] = probe_video(path) if kind["kind"] == "video" else kind


HANDLERS: dict[str, Callable[..., None]] = {"video_post": video_post, "probe": probe}


def run(ctx: JobContext) -> None:
    handler = HANDLERS.get(ctx.spec.handler or "")
    if not handler:
        raise RuntimeError(f"unknown builtin handler {ctx.spec.handler!r}")
    handler(ctx, **ctx.spec.handler_args)


def estimate(spec_handler: str, handler_args: dict, params: dict) -> float | None:
    if spec_handler == "video_post" and params.get("video"):
        try:
            n = probe_video(Path(params["video"]))["video"]["frames"] or 0
        except ValueError:
            return None
        return sum(w for _, w in video_post_plan(params, n, handler_args.get("mode", "upscale")))
    if spec_handler == "probe":
        return 1.0
    return None
