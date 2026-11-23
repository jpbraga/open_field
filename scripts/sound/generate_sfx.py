#!/usr/bin/env python3
"""Text to sound effects for openField's sound services. Runs in tools/sound/.venv (see setup.sh).

Engines:
  moss            MOSS-SoundEffect v2.0, MLX port (mlx-community bf16 weights): 48 kHz mono, up to 30 s.
  stable-audio-3  Stable Audio 3.0 Small SFX through mlx-audio: 44.1 kHz stereo.

Variation i (0-based) uses seed + i, so any single variation can be generated again on its own.
Each sound is post-processed with numpy: DC offset removed, optional silence trim, optional seamless loop
(equal-power crossfade of the tail into the head), peak normalisation to -1 dBFS and short edge fades,
then written as 16-bit PCM WAV: <out-dir>/sound_1.wav ... sound_N.wav.

Progress goes to stdout as "progress <done>/<total>" lines (denoising steps over all variations).
"""
from __future__ import annotations

import argparse
import math
import time
import wave
from pathlib import Path

import numpy as np

PEAK_DBFS = -1.0
SEED_MOD = 2**31


def log(msg: str) -> None:
    print(msg, flush=True)


class Progress:
    def __init__(self, total: int) -> None:
        self.total, self.done = total, 0

    def tick(self, n: int = 1) -> None:
        self.done = min(self.total, self.done + n)
        log(f"progress {self.done}/{self.total}")


# ------------------------------------------------------------------ engines

class Moss:
    max_seconds = 30.0

    def __init__(self, model_dir: str) -> None:
        from moss_sfx_mlx.pipeline_mlx import MossSoundEffectPipeline

        self.pipe = MossSoundEffectPipeline.from_pretrained(model_dir)
        self.max_seconds = float(self.pipe.max_inference_seconds)

    def generate(self, prompt: str, seconds: float, steps: int, cfg: float, seed: int, bar: Progress):
        def steps_with_progress(it):
            for x in it:
                yield x
                bar.tick()

        # The model always denoises its full 30 s window and crops to `seconds` (upstream behaviour).
        audio = self.pipe(prompt=prompt, seconds=seconds, num_inference_steps=steps, cfg_scale=cfg, seed=seed,
                          progress_bar_cmd=steps_with_progress)
        return np.asarray(audio[0], dtype=np.float32).T, self.pipe.sample_rate  # (samples, channels)


class StableAudio3:
    def __init__(self, model_dir: str) -> None:
        from mlx_audio.music.utils import load_model

        self.model = load_model(model_dir)
        self.max_seconds = float(self.model.max_duration)

    def generate(self, prompt: str, seconds: float, steps: int, cfg: float, seed: int, bar: Progress):
        result = next(self.model.generate(prompt, duration=seconds, steps=steps, seed=seed, cfg_scale=cfg))
        audio = np.asarray(result.audio, dtype=np.float32)
        if audio.ndim == 1:
            audio = audio[:, None]
        elif audio.shape[0] <= 2 < audio.shape[1]:
            audio = audio.T
        bar.tick(steps)
        return audio, int(result.sample_rate)


ENGINES = {"moss": Moss, "stable-audio-3": StableAudio3}


# ------------------------------------------------------------------ post-processing

def ramp(n: int) -> np.ndarray:
    return (0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n)))[:, None]


def trim_silence(a: np.ndarray, sr: int, rel_db: float = -45.0, floor_db: float = -60.0) -> tuple[np.ndarray, bool]:
    """Cut leading/trailing stretches quieter than rel_db below the loudest 10 ms window."""
    win = max(1, sr // 100)
    n = len(a) // win
    if n < 3:
        return a, False
    env = np.abs(a).max(axis=1)[: n * win].reshape(n, win)
    rms = np.sqrt((env**2).mean(axis=1))
    peak = float(rms.max())
    if peak <= 0:
        return a, False
    loud = np.nonzero(rms > max(10 ** (floor_db / 20), peak * 10 ** (rel_db / 20)))[0]
    start = max(0, int(loud[0]) * win - sr // 200)          # keep 5 ms before the onset
    end = min(len(a), (int(loud[-1]) + 1) * win + sr // 20)  # and 50 ms of tail
    return a[start:end], start > 0


def make_loop(a: np.ndarray, xf: int) -> np.ndarray:
    """Equal-power crossfade of the last xf samples into the first xf; the result wraps without a seam."""
    if xf < 2 or xf * 2 > len(a):
        return a
    t = np.linspace(0.0, np.pi / 2, xf, endpoint=False)[:, None]
    head = a[:xf] * np.sin(t) + a[len(a) - xf:] * np.cos(t)
    return np.concatenate([head, a[xf: len(a) - xf]])


def finish(a: np.ndarray, sr: int, seconds: float, trim: bool, loop: bool, normalize: bool) -> np.ndarray:
    a = a - a.mean(axis=0, keepdims=True)
    if loop:
        wanted = int(min(1.0, 0.2 * seconds) * sr)
        xf = len(a) - int(round(seconds * sr))
        a = make_loop(a, xf if xf >= wanted // 2 else wanted)  # at the model's maximum the loop ends shorter
    else:
        if trim:
            a, cut_head = trim_silence(a, sr)
            if cut_head:
                n = min(len(a), sr * 3 // 1000)
                a[:n] *= ramp(n)
        n = min(len(a), sr // 100)                            # 10 ms fade-out, no click at the end
        a[len(a) - n:] *= ramp(n)[::-1]
    peak = float(np.abs(a).max()) if len(a) else 0.0
    if normalize and peak > 0:
        a = a * (10 ** (PEAK_DBFS / 20) / peak)
    return np.clip(a, -1.0, 1.0)


def write_wav(path: Path, a: np.ndarray, sr: int) -> None:
    pcm = np.round(a * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(a.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=sorted(ENGINES), required=True)
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--variations", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--steps", type=int, required=True)
    ap.add_argument("--cfg", type=float, required=True)
    ap.add_argument("--trim", choices=["true", "false"], default="true")
    ap.add_argument("--loop", choices=["true", "false"], default="false")
    ap.add_argument("--normalize", choices=["true", "false"], default="true")
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    trim, loop, normalize = args.trim == "true", args.loop == "true", args.normalize == "true"

    t0 = time.perf_counter()
    engine = ENGINES[args.engine](args.model_dir)
    log(f"model loaded in {time.perf_counter() - t0:.1f} s")
    if args.seconds > engine.max_seconds:
        raise SystemExit(f"seconds must be at most {engine.max_seconds:g} for this model")
    # A loop is generated longer by the crossfade (up to 1 s, at most 20%), so it ends at the requested length.
    gen_seconds = args.seconds
    if loop:
        gen_seconds = min(engine.max_seconds, args.seconds + min(1.0, 0.2 * args.seconds))
        if args.engine == "moss":
            gen_seconds = math.floor(gen_seconds * 10 + 1e-6) / 10  # the pipeline works in 0.1 s steps

    args.out_dir.mkdir(parents=True, exist_ok=True)
    bar = Progress(args.steps * args.variations)
    for i in range(args.variations):
        seed = (args.seed + i) % SEED_MOD
        t1 = time.perf_counter()
        raw, sr = engine.generate(args.prompt, gen_seconds, args.steps, args.cfg, seed, bar)
        out = finish(raw, sr, args.seconds, trim, loop, normalize)
        path = args.out_dir / f"sound_{i + 1}.wav"
        write_wav(path, out, sr)
        log(f"sound_{i + 1}: seed {seed}, {len(out) / sr:.2f} s, {sr} Hz, {out.shape[1]} ch, "
            f"generated in {time.perf_counter() - t1:.1f} s")
    try:
        import mlx.core as mx

        log(f"peak memory {mx.get_peak_memory() / 1e9:.1f} GB")
    except Exception:  # noqa: BLE001 - informational only
        pass


if __name__ == "__main__":
    main()
