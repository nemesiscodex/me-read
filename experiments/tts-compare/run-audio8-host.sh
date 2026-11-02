#!/usr/bin/env bash
# Run from ~/me-read-compare on the Radeon host after setup-audio8-host.sh. Usage: run-audio8-host.sh OUT_DIR [DTYPE]
set -eu
cd "${ME_READ_COMPARE_HOME:-$HOME/me-read-compare}"
out="${1:-outputs-audio8}"
dtype="${2:-bfloat16}"
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

mkdir -p "$out"
for language in en es; do
    .venv-audio8/bin/python -u run-audio8.py --language "$language" --model models/audio8-air \
        --reference "$language.wav" --transcript "$language.txt" --dtype "$dtype" \
        --out "$out" > "$out/audio8-$language.log" 2>&1 || true
done
