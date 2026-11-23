"""FLUX.2 [dev] (32B) for openField, in two processes so it fits in 128 GB of unified memory.

  encode    loads only the text encoder (Mistral Small 3.2, ~48 GB in bf16), optionally rewrites the prompt
            with it, turns the prompt into embeddings and exits, which frees all of its memory. Embeddings are
            cached by prompt, so a prompt seen before skips the encoder and its load time.
  generate  loads only the transformer (~64 GB) and the VAE and generates from those embeddings, optionally
            guided by reference images.

Runs in tools/flux2/.venv (see scripts/flux2/setup.sh). Prints "progress <done>/<total>" per denoising step.

  python flux2.py encode --model-dir SNAPSHOT --prompt "a lighthouse at dusk" --out work/embeds.pt \
      --prompt-out work/prompt.txt --cache-dir ~/.cache/openfield/flux2-embeds
  python flux2.py generate --model-dir SNAPSHOT --embeds work/embeds.pt --prompt-file work/prompt.txt \
      --steps 28 --seed 7 --width 1024 --height 1024 --out work/out.png
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

import torch
from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo

DEVICE = "mps"
DTYPE = torch.bfloat16
MODEL_NAME = "FLUX.2-dev"


def say(msg: str) -> None:
    print(msg, flush=True)


def gb(n: float) -> str:
    return f"{n / 1024**3:.1f} GB"


def load_images(paths: list[str]) -> list[Image.Image]:
    """Reference images upright (EXIF orientation) and in RGB; transparent areas become white."""
    out = []
    for p in paths:
        im = ImageOps.exif_transpose(Image.open(p))
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        out.append(im.convert("RGB"))
    return out


def link_or_copy(src: Path, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def prune(cache: Path, keep: int) -> None:
    files = sorted(cache.glob("*.pt"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[keep:]:
        f.unlink(missing_ok=True)


def encode(a: argparse.Namespace) -> None:
    from diffusers import Flux2Pipeline

    model_dir = Path(a.model_dir)
    key = hashlib.sha256(f"{model_dir.name}\0{a.max_sequence_length}\0{a.prompt}".encode()).hexdigest()[:32]
    cache = Path(a.cache_dir) / f"{key}.pt" if a.cache_dir else None
    out = Path(a.out)
    prompt_out = Path(a.prompt_out) if a.prompt_out else None

    if cache and cache.exists() and not a.enhance:
        os.utime(cache)  # most recently used, for pruning
        link_or_copy(cache, out)
        if prompt_out:
            prompt_out.write_text(a.prompt, encoding="utf-8")
        say(f"prompt embeddings cached ({cache.name})")
        return

    t = time.time()
    say("loading the text encoder (Mistral Small 3.2, ~48 GB)...")
    pipe = Flux2Pipeline.from_pretrained(model_dir, transformer=None, vae=None, dtype=DTYPE)
    pipe.text_encoder.to(DEVICE)
    say(f"text encoder loaded in {time.time() - t:.0f}s")

    prompt = a.prompt
    with torch.inference_mode():
        if a.enhance:
            t = time.time()
            refs = load_images(a.images)
            say("enhancing the prompt" + (f" with {len(refs)} reference image(s)" if refs else "") + "...")
            prompt = pipe.upsample_prompt(prompt, images=[refs] if refs else None, temperature=a.temperature,
                                          device=DEVICE)[0].strip().lstrip(":-–— ").strip() or a.prompt
            say(f"enhanced prompt ({time.time() - t:.0f}s): {prompt}")
        t = time.time()
        embeds, _ = pipe.encode_prompt(prompt=prompt, device=DEVICE, max_sequence_length=a.max_sequence_length)
    embeds = embeds.to("cpu")
    peak = torch.mps.driver_allocated_memory()
    del pipe
    gc.collect()

    out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(embeds, out)
    if prompt_out:
        prompt_out.write_text(prompt, encoding="utf-8")
    if cache and not a.enhance:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(f".{os.getpid()}.tmp")
        shutil.copyfile(out, tmp)
        os.replace(tmp, cache)
        prune(cache.parent, a.cache_keep)
    say(f"prompt encoded in {time.time() - t:.1f}s; GPU memory {gb(peak)}")


def generate(a: argparse.Namespace) -> None:
    from diffusers import Flux2Pipeline

    if (a.width is None) != (a.height is None):
        sys.exit("give both --width and --height, or neither")
    refs = load_images(a.images)
    if a.width is None and not refs:
        a.width = a.height = 1024
    prompt = Path(a.prompt_file).read_text(encoding="utf-8") if a.prompt_file else ""
    embeds = torch.load(a.embeds)

    t = time.time()
    say("loading the transformer (~64 GB) and the VAE...")
    pipe = Flux2Pipeline.from_pretrained(a.model_dir, text_encoder=None, tokenizer=None, dtype=DTYPE).to(DEVICE)
    say(f"transformer loaded in {time.time() - t:.0f}s")

    peak = 0

    def on_step(_pipe, i, _t, kwargs):
        nonlocal peak
        # MPS runs ahead asynchronously: without this the loop reports all steps in seconds and then waits.
        torch.mps.synchronize()
        peak = max(peak, torch.mps.driver_allocated_memory())
        say(f"progress {i + 1}/{a.steps}")
        return kwargs

    size = f"{a.width}x{a.height}" if a.width else "the size of image 1"
    say(f"generating at {size}, {a.steps} steps, guidance {a.guidance:g}, seed {a.seed}"
        + (f", {len(refs)} reference image(s)" if refs else ""))
    t = time.time()
    with torch.inference_mode():
        image = pipe(
            image=refs or None,
            prompt_embeds=embeds.to(DEVICE, DTYPE),
            width=a.width,
            height=a.height,
            num_inference_steps=a.steps,
            guidance_scale=a.guidance,
            generator=torch.Generator("cpu").manual_seed(a.seed),
            callback_on_step_end=on_step,
        ).images[0]
    gen = time.time() - t
    peak = max(peak, torch.mps.driver_allocated_memory())

    meta = PngInfo()
    for k, v in (("prompt", prompt), ("seed", a.seed), ("steps", a.steps), ("guidance", a.guidance),
                 ("model", MODEL_NAME), ("references", len(refs)), ("Software", "openField")):
        meta.add_text(k, str(v))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    image.save(a.out, pnginfo=meta)
    say(f"done: {image.width}x{image.height} in {gen:.0f}s ({gen / a.steps:.1f}s/step); "
        f"peak GPU memory {gb(peak)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode", help="prompt -> embeddings (text encoder only)")
    e.add_argument("--model-dir", required=True, help="FLUX.2-dev snapshot folder (diffusers layout)")
    e.add_argument("--prompt", required=True)
    e.add_argument("--out", required=True, help="embeddings file to write")
    e.add_argument("--prompt-out", help="text file for the prompt actually used (the rewritten one with --enhance)")
    e.add_argument("--cache-dir", help="embeddings cache folder (skipped with --enhance)")
    e.add_argument("--cache-keep", type=int, default=64, help="cached prompts to keep (about 15 MB each)")
    e.add_argument("--max-sequence-length", type=int, default=512)
    e.add_argument("--enhance", action="store_true", help="rewrite the prompt in detail with the text encoder first")
    e.add_argument("--temperature", type=float, default=0.15, help="sampling temperature for --enhance")
    e.add_argument("--images", nargs="+", default=[], help="reference images the rewrite should consider")

    g = sub.add_parser("generate", help="embeddings -> image (transformer + VAE only)")
    g.add_argument("--model-dir", required=True)
    g.add_argument("--embeds", required=True)
    g.add_argument("--prompt-file", help="prompt text, stored in the PNG metadata")
    g.add_argument("--out", required=True)
    g.add_argument("--steps", type=int, default=28)
    g.add_argument("--guidance", type=float, default=4.0)
    g.add_argument("--width", type=int)
    g.add_argument("--height", type=int)
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--images", nargs="+", default=[], help="reference images, in order; image 1 sets the size")

    a = ap.parse_args()
    if not torch.backends.mps.is_available():
        sys.exit("the Mac GPU (MPS) is not available to PyTorch")
    encode(a) if a.cmd == "encode" else generate(a)


if __name__ == "__main__":
    main()
