#!/bin/sh
# Sets up the sound-effect services (moss-sfx, stable-audio-sfx) on an Apple Silicon Mac:
# a Python 3.12 environment in tools/sound/.venv (outside git) and the model weights in the Hugging Face cache.
#
#   sh scripts/sound/setup.sh            # environment + both models
#   sh scripts/sound/setup.sh --no-moss  # Stable Audio 3 only
#
# Stable Audio 3.0 Small SFX is distributed under the Stability AI Community License and the Gemma terms;
# MOSS-SoundEffect v2.0 and its MLX port are Apache-2.0.
set -eu

APP=$(cd "$(dirname "$0")/../.." && pwd)
VENV="$APP/tools/sound/.venv"
# MLX port of MOSS-SoundEffect v2.0, pinned to the reviewed commit.
MOSS_PORT="git+https://github.com/xocialize/moss-soundeffect-mlx@4e8e3e36d4d9c991d16a3e65a4b7099d406b9ed8"
WITH_MOSS=1
[ "${1:-}" = "--no-moss" ] && WITH_MOSS=0

[ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ] || { echo "MLX needs an Apple Silicon Mac" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

mkdir -p "$APP/tools/sound"
[ -x "$VENV/bin/python" ] || uv venv --python 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" "mlx-audio==0.6.7" "mlx-lm>=0.31.1" ftfy regex safetensors tqdm
[ "$WITH_MOSS" = 1 ] && uv pip install --python "$VENV/bin/python" "moss-sfx-mlx @ $MOSS_PORT"

uvx --from huggingface_hub hf download mlx-community/stable-audio-3-small-sfx
[ "$WITH_MOSS" = 1 ] && uvx --from huggingface_hub hf download mlx-community/MOSS-SoundEffect-v2.0-bf16

echo "Done. Reload the services: openfield check, then POST /api/v1/admin/reload (or restart the daemon)."
