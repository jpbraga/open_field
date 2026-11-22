"""Media helpers (ffprobe, colour handling) and Mac system readings (memory, GPU processes)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Optional

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
IMAGE_CODECS = {"png", "mjpeg", "webp", "bmp", "tiff"}
AUDIO_ONLY_FORMATS = {"wav", "mp3", "flac", "ogg", "aac", "m4a"}


def _ratio(s: Optional[str]) -> Optional[Fraction]:
    try:
        r = Fraction(s)
        return r if r > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise ValueError(f"{path.name}: not a readable media file ({out.stderr.strip()[-200:]})")
    return json.loads(out.stdout or "{}")


def classify(path: Path) -> dict:
    """Kind (image, video, audio) and basic facts of an uploaded file."""
    data = ffprobe_json(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    frames = video.get("nb_frames") if video else None
    fmt_name = fmt.get("format_name") or ""
    # Still images come through the image2 demuxer or a *_pipe one (png_pipe, webp_pipe...).
    still = bool(video) and video.get("codec_name") in IMAGE_CODECS and (fmt_name == "image2" or fmt_name.endswith("_pipe"))
    if video and still:
        kind = "image"
    elif video:
        kind = "video"
    elif audio:
        kind = "audio"
    else:
        raise ValueError(f"{path.name}: no picture or sound found")
    info: dict[str, Any] = {"kind": kind, "size_bytes": path.stat().st_size}
    if video:
        info.update(width=video.get("width"), height=video.get("height"), codec=video.get("codec_name"))
    if kind == "video":
        fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
        info.update(fps=float(fps) if fps else None,
                    duration_s=float(fmt["duration"]) if fmt.get("duration") else None,
                    frames=int(frames) if str(frames or "").isdigit() else None, audio=bool(audio))
    if kind == "audio":
        info.update(codec=audio.get("codec_name"),
                    duration_s=float(fmt["duration"]) if fmt.get("duration") else None)
    return info


def probe_video(path: Path) -> dict:
    """Codec, size, fps, frame count, duration, colour tags and audio streams of a video."""
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not video:
        raise ValueError("no video stream in the file")
    fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
    frames = int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None
    if frames is None:
        cnt = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
                              "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=600)
        frames = int(cnt.stdout.strip().split(",")[0]) if cnt.stdout.strip() else None
    return {
        "container": fmt.get("format_name"),
        "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
        "size_bytes": int(fmt["size"]) if fmt.get("size") else None,
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "video": {
            "codec": video.get("codec_name"), "profile": video.get("profile"),
            "width": video.get("width"), "height": video.get("height"),
            "fps": float(fps) if fps else None, "fps_fraction": str(fps) if fps else None,
            "frames": frames, "pix_fmt": video.get("pix_fmt"),
            "color_space": video.get("color_space"), "color_primaries": video.get("color_primaries"),
            "color_transfer": video.get("color_transfer"), "color_range": video.get("color_range"),
        },
        "audio": [{
            "codec": a.get("codec_name"), "sample_rate": int(a["sample_rate"]) if a.get("sample_rate") else None,
            "channels": a.get("channels"), "channel_layout": a.get("channel_layout"),
            "bit_rate": int(a["bit_rate"]) if a.get("bit_rate") else None,
            "duration_s": float(a["duration"]) if a.get("duration") else None,
        } for a in audio],
    }


def describe_output(path: Path, media_type: str) -> dict:
    """Facts about a produced file, for the job result and the web UI."""
    info: dict[str, Any] = {"size_bytes": path.stat().st_size, "media_type": media_type}
    try:
        if media_type.startswith("image/"):
            c = classify(path)
            info.update(width=c.get("width"), height=c.get("height"))
        elif media_type.startswith("video/"):
            p = probe_video(path)
            info.update(width=p["video"]["width"], height=p["video"]["height"], frames=p["video"]["frames"],
                        fps=p["video"]["fps"], duration_s=p["duration_s"], audio=bool(p["audio"]))
    except (ValueError, subprocess.SubprocessError):
        pass
    return info


# ffprobe colour-matrix names -> swscale matrix names.
_SWS_MATRIX = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470",
               "bt2020nc": "bt2020", "smpte240m": "smpte240m", "fcc": "fcc"}


def color_plan(info: dict) -> dict:
    """Use the source's YUV matrix both ways, so the RGB round trip does not shift colours.
    Untagged HD video is treated as BT.709 (swscale would otherwise assume BT.601)."""
    v = info["video"]
    space = v.get("color_space") if v.get("color_space") in _SWS_MATRIX else (
        "bt709" if (v.get("height") or 0) >= 720 else "smpte170m")
    rng = "pc" if v.get("color_range") == "pc" else "tv"
    prim, trc = v.get("color_primaries"), v.get("color_transfer")
    if space == "bt709":
        prim = prim if prim not in (None, "unknown") else "bt709"
        trc = trc if trc not in (None, "unknown") else "bt709"
    # ffmpeg 7+ takes the encoder's colour tags from the frames, so they are set in the filter graph.
    params = [f"colorspace={space}", f"range={rng}"]
    if prim and prim != "unknown":
        params.append(f"color_primaries={prim}")
    if trc and trc != "unknown":
        params.append(f"color_trc={trc}")
    return {"matrix": _SWS_MATRIX[space], "range": rng, "setparams": "setparams=" + ":".join(params)}


def available_memory_gb() -> float:
    """Memory the system can hand out now: free, inactive, speculative and purgeable pages."""
    out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True).stdout
    m = re.search(r"page size of (\d+)", out)
    page = int(m.group(1)) if m else 16384

    def pages(name: str) -> int:
        hit = re.search(rf"^{name}:\s+(\d+)", out, re.M)
        return int(hit.group(1)) if hit else 0

    total = pages("Pages free") + pages("Pages inactive") + pages("Pages speculative") + pages("Pages purgeable")
    return round(total * page / 1024**3, 1)


def gpu_name() -> Optional[str]:
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def find_processes(names: Iterable[str], exclude_pids: set[int]) -> list[str]:
    """Running processes whose executable (or Python/shell script) matches one of the glob names,
    leaving out exclude_pids and their descendants (the daemon's own tools)."""
    import fnmatch

    patterns = list(names)
    if not patterns:
        return []
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    parent = {pid: ppid for pid, ppid, _ in rows}

    def ours(pid: int) -> bool:
        seen = 0
        while pid and seen < 64:
            if pid in exclude_pids:
                return True
            pid, seen = parent.get(pid, 0), seen + 1
        return False

    found = []
    for pid, _, cmd in rows:
        argv = cmd.split()
        if not argv or ours(pid):
            continue
        candidates = [Path(argv[0]).name]
        if re.match(r"^(python[\d.]*|sh|bash|zsh)$", candidates[0]) and len(argv) > 1:
            candidates.append(Path(argv[1]).name)
        hit = next((c for c in candidates if any(fnmatch.fnmatch(c, p) for p in patterns)), None)
        if hit:
            found.append(hit)
    return found
