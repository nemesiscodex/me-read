# speech-service

The speech server for `--tts-provider breeze-gguf`. It keeps Breeze TTS 2 loaded on a GPU and clones whichever reference voice each request names. It is CrispASR with one patch, and it is not an image of this repository's Python code. It runs on the machine with the GPU, not on the machine that runs me-read.

Built and tested on Linux with an AMD Radeon (gfx1030, 16 GB) and ROCm 6.4. One resident server answers a 5.7 second line in about 9 seconds with the q4_k model. That is slower than real time. The patched server, the tested model and the me-read client were run together once, for 4 requests.

## What is patched

`per-request-voice.patch` changes one file of CrispASR `c1c65235975aa8827c52271286de08d3d166cf0e`, `examples/cli/crispasr_backend_bt2_tts.cpp`. Without it the Breeze backend reads its reference voice once at startup and ignores the `voice` in each request, so every character would sound the same.

With it, a request that names a voice uses that voice's WAV from `--voice-dir` and the transcript in the request or in the `<name>.txt` beside it. The decoded WAV is cached by the SHA-256 of its bytes, at most 16 at a time. A voice that cannot be read, or has no transcript, fails the request. It never falls back to another voice. The watermark, provenance record and consent code are untouched.

Breeze's own voice design from a description is not available in this runtime, so me-read designs new voices locally with Qwen and this server only clones them.

## Build

Needs ROCm with `hipcc`, `cmake`, `clang` and the hipBLAS and rocBLAS CMake packages. Run on the GPU machine as an ordinary user.

```sh
git clone https://github.com/CrispStrobe/CrispASR ~/me-read-speech/src/crispasr
cd ~/me-read-speech/src/crispasr
git checkout c1c65235975aa8827c52271286de08d3d166cf0e
git submodule update --init --recursive
git apply /path/to/speech-service/per-request-voice.patch
export PATH=/opt/rocm/bin:/opt/rocm/llvm/bin:$PATH
export HIPCXX="$(/opt/rocm/bin/hipconfig -l)/clang"
export HIP_PATH="$(/opt/rocm/bin/hipconfig -R)"
cmake -S . -B ~/me-read-speech/build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_HIP_FLAGS="-I/opt/rocm/include --rocm-path=/opt/rocm" \
  -DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1030 -DCMAKE_HIP_ARCHITECTURES=gfx1030 \
  -DCMAKE_C_COMPILER=/usr/bin/clang -DCMAKE_CXX_COMPILER=/usr/bin/clang++ \
  -DCRISPASR_BUILD_TESTS=OFF -DCRISPASR_BUILD_EXAMPLES=ON -DCRISPASR_BUILD_SERVER=OFF \
  -DCRISPASR_CURL=OFF -DCRISPASR_FFMPEG=OFF
cmake --build ~/me-read-speech/build -j"$(nproc)" --target crispasr-cli
```

The test machine had no hipBLAS or rocBLAS CMake packages. The tested build unpacked the matching 6.4.4 packages from the Arch Linux Archive into a private folder and pointed `CMAKE_PREFIX_PATH` and `ROCM_ROOT` at it, without installing anything system wide. Adjust the flags for your ROCm and GPU. A different GPU has not been tried.

Download `breeze-tts-2-q4_k.gguf` from `cstr/breeze-tts-2-GGUF` and `qwen3-tts-tokenizer-12hz.gguf` from `cstr/qwen3-tts-tokenizer-12hz-GGUF` into `~/me-read-speech/models/`. The revisions tested were `5314fe9ae3ec6eac1a9238c4f6888fdc9fda0e83` and `344e279551177654762e5c7d39e217834325f222`.

## Run

```sh
mkdir -p ~/me-read-speech/voices
BIN=~/me-read-speech/build/bin/crispasr MODEL=~/me-read-speech/models/breeze-tts-2-q4_k.gguf \
VOICE_DIR=~/me-read-speech/voices ./run-speech-service.sh
```

It listens on `127.0.0.1:8781`. It has no login, so keep it on localhost and reach it from the me-read machine through an SSH tunnel.

```sh
ssh -N -L 8781:127.0.0.1:8781 HOST
```

Then start me-read with `--tts-url http://127.0.0.1:8781`. To listen on the network instead, set `HOST=0.0.0.0`. Anyone who can reach the port can then use the GPU and upload voices.

`--accept-license other` accepts the BreezeBlue Research and Non-Commercial License for the weights. `--accept-marking-responsibility` lets each request turn the spoken AI disclaimer off, and you then disclose the audio as AI-generated. The weights are for non-commercial research use.

## Sharing the GPU with the image service

On a 16 GB card the image service needs nearly all the memory. With this server loaded, an image request failed with `HIP out of memory`. Run one at a time. `me-read-speech.service` is a template user unit with `Conflicts=me-read-imager.service`, so starting either one stops the other. That `Conflicts` line has not been tried. Nothing here installs the unit.

## HTTP contract

- `POST /v1/voices` uploads a reference as multipart form data with `voice` (the WAV), `name` (letters, digits, `-` and `_`), `consent_attestation` (required) and `transcript`. It answers 201, or 409 when the name exists.
- `POST /v1/audio/speech` takes JSON with `input`, `voice`, `ref_text`, `seed`, `response_format` (`wav`), `consent_attestation` (required for a clone) and `spoken_disclaimer` (set `false`). It answers 200 with a 24 kHz mono 16-bit WAV.
- A missing consent statement is HTTP 400 `consent_required`. A voice that is unknown, unreadable or has no transcript is HTTP 500 `synthesis_failed`, with the reason only in the server log.
- The server answers one request at a time.
