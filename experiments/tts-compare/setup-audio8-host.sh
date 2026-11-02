#!/usr/bin/env bash
# Prepare an isolated Audio8 environment using the speech service's ROCm PyTorch. Does not touch the GPU.
set -eu
cd "${ME_READ_COMPARE_HOME:-$HOME/me-read-compare}"
model_revision=23c305cc013fde378edb459f538317043c993a74
speech_python="$HOME/me-read-speech/.venv/bin/python"
if [ ! -d .venv-audio8 ]; then
    uv venv --python "$speech_python" .venv-audio8
fi
speech_site="$("$speech_python" -c 'import site; print(site.getsitepackages()[0])')"
audio8_site="$(.venv-audio8/bin/python -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n' "$speech_site" > "$audio8_site/rocm-torch.pth"
uv pip install --python .venv-audio8/bin/python 'transformers>=4.57.0,<5' 'soundfile>=0.12' 'safetensors>=0.4' huggingface_hub numpy
.venv-audio8/bin/python - "$model_revision" <<'PY'
from huggingface_hub import snapshot_download
import sys
snapshot_download('Edge0/Audio8-TTS-Realistic-Air', revision=sys.argv[1],
                  local_dir='models/audio8-air', ignore_patterns=['assets/*', '*.jpeg'])
PY
.venv-audio8/bin/python -c 'import torch, transformers; print(torch.__version__, transformers.__version__)'
