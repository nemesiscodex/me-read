#!/usr/bin/env bash
# Run from ~/me-read-compare on the prepared Radeon host.
set -eu
cd "${ME_READ_COMPARE_HOME:-$HOME/me-read-compare}"
out="${1:-outputs}"
if [ -e "$out" ]; then
    echo "Choose a new output directory: $out already exists" >&2
    exit 1
fi
was_active=0
if systemctl --user is-active --quiet me-read-speech; then
    was_active=1
fi
restore_service() {
    if [ "$was_active" = 1 ]; then
        systemctl --user start me-read-speech
    fi
}
trap restore_service EXIT
trap 'exit 130' INT TERM
systemctl --user stop me-read-speech

# Each model has exclusive use of the GPU. Choose a new output directory to rerun.
mkdir -p "$out"
for language in en es; do
    model="$HOME/me-read-speech/models/chatterbox-turbo"
    if [ "$language" = es ]; then
        model="$HOME/me-read-speech/models/chatterbox-multilingual"
    fi
    "$HOME/me-read-speech/.venv/bin/python" -u run.py --engine chatterbox --language "$language" \
        --model "$model" --reference "$language.wav" --transcript "$language.txt" \
        --out "$out" > "$out/chatterbox-$language.log" 2>&1
    .venv/bin/python -u run.py --engine ming --language "$language" \
        --ming-source Ming-3.0-tts --model models/ming \
        --reference "$language.wav" --transcript "$language.txt" \
        --out "$out" > "$out/ming-$language.log" 2>&1
done
