# speech-service

The speech service for me-read. It runs on the machine with the GPU and does me-read's heavy speech work there instead of on the Mac:

- `breeze`: Breeze TTS 2 designs English voices from a description, clones a voice, and directs how a line is said (the emotion takes).
- `qwen`: Qwen3-TTS 1.7B VoiceDesign designs voices for books in other languages.
- `chatterbox`: Chatterbox narrates in an uploaded voice. English uses Chatterbox Turbo, other languages Chatterbox Multilingual.

Breeze and Qwen run in [CrispASR](https://github.com/CrispStrobe/CrispASR) processes that the service starts and stops. Chatterbox runs inside the service with the ROCm build of PyTorch. Breeze and Qwen in PyTorch were tried and rejected: Breeze took 8 to 9 seconds of work per second of audio (and fp16 failed), Qwen 4.5 to 6.6.

Written for and tested on Linux with an AMD Radeon RX 6900 XT (gfx1030, 16 GB) and ROCm.

## Speed

Measured on that card, in seconds of work per second of audio, one request at a time:

| request | speed |
|---|---|
| `breeze` design (`cfg_scale` 4) | 1.4 |
| `breeze` clone | 1.5 |
| `breeze` direction (`cfg_scale` 4) | 1.9 on a 5 second line |
| `qwen` design | 0.8 |
| `chatterbox` English (Turbo) | 0.3, and about 1.0 for the first line in a new voice |
| `chatterbox` other languages (Multilingual) | 0.8 |

Loading a model on first use adds 1 to 2 seconds for Breeze or Qwen, and 5 to 15 seconds for a Chatterbox model.

Two settings matter for these numbers, and the service sets both itself. `MIOPEN_FIND_MODE=FAST` stops PyTorch searching for new kernels for every new sentence length. Without it Chatterbox was about three times slower on lines it had not seen before. `GPU_MAX_HW_QUEUES=1` keeps the CrispASR processes and PyTorch from running out of hardware queues on the card. Without it Turbo slowed from 0.26 to 0.74 once both CrispASR processes were loaded.

## Build CrispASR

Needs ROCm with `hipcc`, `cmake`, `clang` and the hipBLAS and rocBLAS CMake packages. Use commit `3386096f` with `crispasr-breeze-one-prompt.patch` from this folder. That commit reads the voice named in each request from `--voice-dir` and supports Breeze's instructions and `cfg_scale`. Its server sends Breeze each sentence as a separate prompt, though, so a designed voice changes speaker at every sentence. The patch packs sentences into prompts of up to 600 characters, as Breeze itself does.

```sh
git clone https://github.com/CrispStrobe/CrispASR ~/me-read-speech/src/crispasr
cd ~/me-read-speech/src/crispasr
git checkout 3386096f
git apply /path/to/me-read/speech-service/crispasr-breeze-one-prompt.patch
git submodule update --init --recursive
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

The test machine had no hipBLAS or rocBLAS CMake packages. The tested build unpacked matching packages into a private folder, added it to `CMAKE_PREFIX_PATH`, and gives CrispASR that folder's `lib` at run time with `--crispasr-library-path`. Adjust the flags for your ROCm and GPU. A different GPU has not been tried.

## Download the models

Put them in `~/me-read-speech/models/`:

| file or folder | from | revision tested |
|---|---|---|
| `breeze-tts-2-q4_k.gguf` | `cstr/breeze-tts-2-GGUF` | `5314fe9ae3ec6eac1a9238c4f6888fdc9fda0e83` |
| `qwen3-tts-tokenizer-12hz.gguf` | `cstr/qwen3-tts-tokenizer-12hz-GGUF` | `344e279551177654762e5c7d39e217834325f222` |
| `qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf` | `cstr/qwen3-tts-1.7b-voicedesign-GGUF` | `e3646df398d00b77f0e907a19a44f1a3723af14d` |
| `chatterbox-turbo/` | `ResembleAI/chatterbox-turbo` | `749d1c1a46eb10492095d68fbcf55691ccf137cd` |
| `chatterbox-multilingual/` | `ResembleAI/chatterbox` | `5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18` |

```sh
cd ~/me-read-speech/models
hf() { uvx --from huggingface_hub hf download "$@"; }
hf cstr/breeze-tts-2-GGUF breeze-tts-2-q4_k.gguf --revision 5314fe9ae3ec6eac1a9238c4f6888fdc9fda0e83 --local-dir .
hf cstr/qwen3-tts-tokenizer-12hz-GGUF qwen3-tts-tokenizer-12hz.gguf --revision 344e279551177654762e5c7d39e217834325f222 --local-dir .
hf cstr/qwen3-tts-1.7b-voicedesign-GGUF qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf --revision e3646df398d00b77f0e907a19a44f1a3723af14d --local-dir .
hf ResembleAI/chatterbox-turbo --revision 749d1c1a46eb10492095d68fbcf55691ccf137cd --local-dir chatterbox-turbo \
  --include '*.safetensors' --include '*.json' --include '*.txt' --include conds.pt
hf ResembleAI/chatterbox --revision 5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18 --local-dir chatterbox-multilingual \
  --include ve.pt --include s3gen.pt --include t3_mtl23ls_v3.safetensors \
  --include grapheme_mtl_merged_expanded_v1.json --include Cangjie5_TC.json --include conds.pt
```

## Run

Needs [uv](https://docs.astral.sh/uv/) and a user in the `video` and `render` groups. Copy this folder to `~/me-read-speech`, then:

```sh
cd ~/me-read-speech
uv run --frozen python server.py
```

The first run installs PyTorch 2.9.1 for ROCm 6.4 and Chatterbox at commit `5de7a54`. Chatterbox asks for PyTorch 2.6.0, which has no build for this ROCm, so `pyproject.toml` overrides it with the version that was measured.

Flags, all optional:

| flag | default | meaning |
|---|---|---|
| `--host`, `--port` | `127.0.0.1`, `8781` | where it listens |
| `--voices` | `~/me-read-speech/voices` | where uploaded voices are kept |
| `--models` | `~/me-read-speech/models` | the model files above |
| `--crispasr` | `~/me-read-speech/build/bin/crispasr` | the CrispASR binary |
| `--crispasr-library-path` | none | `LD_LIBRARY_PATH` for CrispASR only, for example `/opt/rocm/lib`. PyTorch brings its own ROCm libraries. |
| `--budget-gb` | `13` | how much GPU memory the loaded models may use, by estimate |
| `--load-wait` | `60` | seconds a request waits for its model to load before it gets a 503 |

It has no login, so keep it on localhost and reach it from the Mac through an SSH tunnel:

```sh
ssh -N -L 8781:127.0.0.1:8781 radeon
```

Then start me-read with `--tts-url http://127.0.0.1:8781` and your own `--tts-consent` statement.

To keep it running across logins and reboots, install it as a user service. `me-read-speech.service` gives CrispASR `/opt/rocm/lib`. If your hipBLAS and rocBLAS are elsewhere, override that in a drop-in, as the test machine does:

```sh
cp me-read-speech.service ~/.config/systemd/user/
mkdir -p ~/.config/systemd/user/me-read-speech.service.d
printf '[Service]\nEnvironment=CRISPASR_LIBRARY_PATH=%%h/me-read-rocm72-Z1wb/prefix/opt/rocm/lib:/opt/rocm/lib\n' \
  > ~/.config/systemd/user/me-read-speech.service.d/rocm.conf
systemctl --user daemon-reload
systemctl --user enable --now me-read-speech
loginctl enable-linger "$USER"
```

`check.py` exercises every endpoint against a running service and saves the audio it gets back in a folder:

```sh
uv run --frozen python check.py http://127.0.0.1:8781 /tmp/speech-check EN.wav "English transcript" ES.wav "Spanish transcript"
```

## GPU memory

The four models do not fit on a 16 GB card together. The service loads each one on first use. Before loading, it unloads the least recently used model until the estimated total fits `--budget-gb`. A CrispASR model is unloaded by stopping its process, and a Chatterbox model by dropping it and emptying PyTorch's cache.

| model | estimate |
|---|---|
| Breeze (CrispASR) | 3.3 GB |
| Qwen (CrispASR) | 2.9 GB |
| Chatterbox Turbo | 4.5 GB |
| Chatterbox Multilingual | 5.5 GB |

With the default 13 GB, any three fit except Breeze, Turbo and Multilingual together, and loading the fourth model always unloads one.

## Sharing the GPU with the image service

The image service needs nearly all of the card. `me-read-speech.service` has `Conflicts=me-read-imager.service`, so starting either one stops the other.

## HTTP contract

Every error is JSON, `{"error": {"code": "...", "message": "..."}}`. A 503 means "not now, retry": a model is still loading. The service answers one speech request at a time and queues the rest.

### `GET /health`

Answers `{"ready": true, "loaded": ["breeze", "chatterbox-turbo"], "loading": null}`. `loaded` lists the models in memory, least recently used first. The names are `breeze`, `qwen`, `chatterbox-turbo` and `chatterbox-multilingual`. `loading` names a model that is still loading.

### `POST /v1/voices`

Multipart form data:

- `voice`: the WAV file.
- `name`: 1 to 64 letters, digits, `-` and `_`.
- `consent_attestation`: your statement that you may use this voice. Required.
- `transcript`: what the WAV says. Optional. When given, it is saved as `<name>.txt`, and `breeze` uses it when a request has no `ref_text`.

Answers 201 when saved and 409 `voice_exists` when the name is taken. A missing consent is 400 `consent_required`, and a file that is not a WAV is 400 `invalid_audio`.

### `POST /v1/audio/speech`

JSON:

| field | meaning |
|---|---|
| `model` | `breeze`, `qwen` or `chatterbox`. Required. |
| `input` | the text to speak. Required. |
| `voice` | an uploaded voice. Required for `chatterbox`. With `breeze` it makes a clone, or a direction when `instructions` is also given. Not allowed with `qwen`. |
| `ref_text` | what the voice's WAV says, for `breeze`. Without it the uploaded transcript is used. |
| `instructions` | a voice description (design) or how to say the line (direction). Required for `qwen`. With `breeze`, no voice means design and a voice means direction. Not allowed with `chatterbox`. |
| `cfg_scale` | `breeze` only. me-read sends 4 when it designs a voice and 1 when it directs a take. |
| `language` | ISO code such as `en` or `es`. `chatterbox` uses Turbo for `en`, which is the default, and Multilingual otherwise. `qwen` speaks it. |
| `seed` | integer. The same seed and inputs give the same audio samples. |
| `consent_attestation` | required whenever `voice` is given. Sent to CrispASR as both its consent and its marking statement. |
| `spoken_disclaimer` | me-read sends `false`. Passed to CrispASR. |
| `response_format` | `wav` |

Answers 200 with a 24 kHz mono 16-bit WAV. When the request names a voice, the reply has `X-Crispasr-Spoken-Disclaimer: skipped`. If CrispASR ever put its spoken disclaimer into the audio, the service answers 502 `disclaimer_applied` instead of returning it.

Errors:

| status and code | when |
|---|---|
| 400 `unknown_model` | `model` is not one of the three |
| 400 `missing_required_field` | a field the model needs is missing |
| 400 `invalid_request` | a field the model does not take is given, or a field is malformed |
| 400 `consent_required` | `voice` without `consent_attestation` |
| 400 `ref_text_required` | `breeze` with a voice, no `ref_text` and no uploaded transcript |
| 400 `unsupported_language` | `chatterbox` does not speak `language` |
| 500 `synthesis_failed` | the voice is not uploaded, or the model failed. me-read uploads the voice again on this. |
| 500 `load_failed` | the model could not load |
| 503 `loading` | the model is still loading |

Chatterbox mirrors me-read's own calls. Turbo reads the voice with `exaggeration=0` and normalised loudness, then generates with `temperature=0.8, top_p=0.95, top_k=1000, repetition_penalty=1.2`. Multilingual reads the voice with its defaults and generates with `language_id`. Both are seeded with `torch.manual_seed(seed)`. Each model reads a voice once and keeps it until the model is unloaded. Each request is one attempt, and me-read decides whether to retake.

### `POST /v1/speaker`

JSON `{"audio": "<base64 WAV>", "language": "en"}`. Answers `{"embedding": [256 floats]}`, the voice as the Chatterbox voice encoder for that language hears it: resampled to 16 kHz, averaged and scaled to length 1. me-read uses it to tell whether a newly designed voice sounds like one the book already has. In the tests, a voice and Turbo's narration in that voice scored 0.91, and the English and Spanish test voices scored 0.75 against each other.

## Licences

The Breeze TTS 2 weights are under the BreezeBlue Research and Non-Commercial License. The service starts Breeze's CrispASR with `--accept-license other` to accept it, so use them for non-commercial work only. It also starts CrispASR with `--accept-marking-responsibility`, which lets each request leave out the spoken AI disclaimer. You then take on telling listeners that the audio is AI-generated. CrispASR's watermark and provenance record stay on, and Chatterbox adds its own watermark. Every request that uses a voice must carry your consent statement, and the service logs it.
