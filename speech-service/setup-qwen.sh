#!/usr/bin/env bash
# Install the original Qwen3-TTS 12Hz 0.6B Base for the speech service: its own Python environment, borrowing the
# service's ROCm PyTorch, and the checkpoint. Run it from the service folder after the service has run once.
set -eu
cd "${ME_READ_SPEECH_HOME:-$HOME/me-read-speech}"
revision=5d83992436eae1d760afd27aff78a71d676296fc
service_python="$PWD/.venv/bin/python"
if [ ! -x "$service_python" ]; then
    echo "Run the service once first so that $PWD/.venv exists (uv run --frozen python server.py)." >&2
    exit 1
fi
if [ ! -d .venv-qwen ]; then
    uv venv --python "$service_python" .venv-qwen
fi
service_site="$("$service_python" -c 'import site; print(site.getsitepackages()[0])')"
qwen_site="$(.venv-qwen/bin/python -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n' "$service_site" > "$qwen_site/service-torch.pth"
# accelerate and qwen-tts list torch as a requirement, and installing them with it would put a CUDA build here.
uv pip install --python .venv-qwen/bin/python --no-deps 'accelerate==1.12.0' qwen-tts
uv pip install --python .venv-qwen/bin/python 'transformers==4.57.3' librosa 'soundfile>=0.12' sox onnxruntime einops \
    huggingface_hub numpy psutil pyyaml
.venv-qwen/bin/python - "$revision" <<'PY'
from huggingface_hub import snapshot_download
import sys
snapshot_download('Qwen/Qwen3-TTS-12Hz-0.6B-Base', revision=sys.argv[1], local_dir='models/qwen3-tts-base')
PY
.venv-qwen/bin/python -c 'import torch, transformers; print("torch", torch.__version__, "transformers", transformers.__version__)' 2>&1 | tail -1
