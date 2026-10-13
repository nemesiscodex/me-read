#!/usr/bin/env bash
# Start the patched CrispASR speech server with Breeze TTS 2 (GGUF q4_k) loaded once.
#
# BIN         the patched crispasr binary (required)
# MODEL       breeze-tts-2-q4_k.gguf, with the qwen3-tts-tokenizer-12hz.gguf codec in the same folder (required)
# VOICE_DIR   folder that holds uploaded voices as <name>.wav and <name>.txt (required)
# ROCM_ROOT   folder that holds rocm-dev/opt/rocm/lib when ROCm libraries were unpacked beside the build (optional)
# HOST, PORT  bind address and port (default 127.0.0.1 and 8781)
#
# --accept-license other accepts the BreezeBlue Research and Non-Commercial License for the weights.
# --accept-marking-responsibility lets each request turn the spoken AI disclaimer off. You then disclose
# the audio as AI-generated. The watermark and provenance record stay on.
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8781}"
: "${BIN:?set BIN to the patched crispasr binary}"
: "${MODEL:?set MODEL to breeze-tts-2-q4_k.gguf}"
: "${VOICE_DIR:?set VOICE_DIR to the voice folder}"

if [[ -n "${ROCM_ROOT:-}" ]]; then
  export LD_LIBRARY_PATH="$ROCM_ROOT/rocm-dev/opt/rocm/lib:/opt/rocm/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

exec "$BIN" --server --host "$HOST" --port "$PORT" \
  --backend bt2-tts -m "$MODEL" --accept-license other \
  --voice-dir "$VOICE_DIR" --accept-marking-responsibility -v
