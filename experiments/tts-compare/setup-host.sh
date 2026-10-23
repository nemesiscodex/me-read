#!/usr/bin/env bash
# Prepare an isolated Ming environment using the existing speech service's ROCm runtime.
set -eu
cd "${ME_READ_COMPARE_HOME:-$HOME/me-read-compare}"
source_revision=028c1eace193e7acdb256453da89b6e9207e052b
model_revision=8d66b70a00f05ad183fa8a863b4ce609fc83cbc0
if [ ! -d Ming-3.0-tts ]; then
    git clone https://github.com/inclusionAI/Ming-3.0-tts.git
fi
git -C Ming-3.0-tts checkout "$source_revision"
speech_python="$HOME/me-read-speech/.venv/bin/python"
if [ ! -d .venv ]; then
    uv venv --python "$speech_python" .venv
fi
speech_site="$("$speech_python" -c 'import site; print(site.getsitepackages()[0])')"
compare_site="$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n' "$speech_site" > "$compare_site/chatterbox-rocm.pth"
uv pip install --python .venv/bin/python --no-deps -r requirements-ming-rocm.txt
.venv/bin/python - "$model_revision" <<'PY'
from huggingface_hub import snapshot_download
import sys
snapshot_download('inclusionAI/Ming-3.0-tts-0.5B', revision=sys.argv[1],
                  local_dir='models/ming', ignore_patterns=['figures/*', '*.pdf'])
PY
