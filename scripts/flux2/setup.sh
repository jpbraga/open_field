#!/bin/sh
# Sets up the flux2-image service (FLUX.2 [dev], 32B) on an Apple Silicon Mac with 128 GB:
# a Python 3.12 environment in tools/flux2/.venv (outside git) with diffusers on PyTorch (MPS),
# and the model in the Hugging Face cache (~105 GB, diffusers files only).
#
#   sh scripts/flux2/setup.sh             # environment + model
#   sh scripts/flux2/setup.sh --no-model  # environment only (the model is already in the cache)
#
# The model is gated: accept the FLUX [dev] Non-Commercial License at
# https://huggingface.co/black-forest-labs/FLUX.2-dev and log in first (uvx --from huggingface_hub hf auth login).
# Re-running resumes an interrupted download.
set -eu

APP=$(cd "$(dirname "$0")/../.." && pwd)
VENV="$APP/tools/flux2/.venv"
WITH_MODEL=1
[ "${1:-}" = "--no-model" ] && WITH_MODEL=0

[ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ] || { echo "FLUX.2 on MPS needs an Apple Silicon Mac" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

mkdir -p "$APP/tools/flux2"
[ -x "$VENV/bin/python" ] || uv venv --python 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" "torch==2.15.0" "torchvision==0.29.2" "diffusers==0.42.1" \
  "transformers==5.22.1" "accelerate==1.15.0" safetensors sentencepiece protobuf pillow

if [ "$WITH_MODEL" = 1 ]; then
  "$VENV/bin/hf" download black-forest-labs/FLUX.2-dev \
    --include "model_index.json" --include "scheduler/*" --include "tokenizer/*" \
    --include "text_encoder/*" --include "transformer/*" --include "vae/*"
fi

echo "Done. Reload the services: openfield check, then POST /api/v1/admin/reload (or restart the daemon)."
