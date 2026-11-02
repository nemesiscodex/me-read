#!/usr/bin/env bash
# Install Audio8 for the speech service: its own Python environment, borrowing the service's ROCm PyTorch, and the checkpoint.
# Run it from the service folder after the service has run once, so that .venv exists.
set -eu
cd "${ME_READ_SPEECH_HOME:-$HOME/me-read-speech}"
revision=23c305cc013fde378edb459f538317043c993a74
service_python="$PWD/.venv/bin/python"
if [ ! -x "$service_python" ]; then
    echo "Run the service once first so that $PWD/.venv exists (uv run --frozen python server.py)." >&2
    exit 1
fi
if [ ! -d .venv-audio8 ]; then
    uv venv --python "$service_python" .venv-audio8
fi
service_site="$("$service_python" -c 'import site; print(site.getsitepackages()[0])')"
audio8_site="$(.venv-audio8/bin/python -c 'import site; print(site.getsitepackages()[0])')"
# The service's packages come after Audio8's own, so Audio8 keeps transformers below 5 and borrows torch.
printf '%s\n' "$service_site" > "$audio8_site/service-torch.pth"
uv pip install --python .venv-audio8/bin/python 'transformers>=4.57.0,<5' 'soundfile>=0.12' 'safetensors>=0.4' huggingface_hub numpy
.venv-audio8/bin/python - "$revision" <<'PY'
from huggingface_hub import snapshot_download
import sys
snapshot_download('Edge0/Audio8-TTS-Realistic-Air', revision=sys.argv[1],
                  local_dir='models/audio8-air', ignore_patterns=['assets/*', '*.jpeg'])
PY
.venv-audio8/bin/python -c 'import torch, transformers; print("torch", torch.__version__, "transformers", transformers.__version__)'
