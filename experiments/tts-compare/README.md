# Try Ming against me-read's Chatterbox narrators

This experiment makes six pairs of listening samples from the same text and saved voices. English uses Chatterbox Turbo. Spanish uses Chatterbox Multilingual v3. Ming uses the 0.5B checkpoint for both languages.

The experiment runs outside the reader. It does not add a narrator option or replace any book audio.

## Prepare the Radeon host

Use the existing `~/me-read-speech` installation with Python 3.12 and PyTorch 2.9.1 for ROCm 6.4. The scripts borrow that environment's installed packages, then override Ming's dependencies in a separate environment. They do not change the speech service environment.

From this repository on the Mac:

```sh
ssh radeon 'mkdir -p ~/me-read-compare'
scp experiments/tts-compare/*.py experiments/tts-compare/*.sh \
  experiments/tts-compare/*.json experiments/tts-compare/*.txt radeon:me-read-compare/
ssh radeon 'cd ~/me-read-compare && bash setup-host.sh'
```

The setup pins Ming source to `028c1eace193e7acdb256453da89b6e9207e052b` and its checkpoint to `8d66b70a00f05ad183fa8a863b4ce609fc83cbc0`. The Chatterbox installation uses commit `5de7a54aa4e5e2baadb0182dde554908b48b85c2`. Its checkpoint metadata confirms Turbo revision `749d1c1a46eb10492095d68fbcf55691ccf137cd` and Multilingual revision `5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18`.

Place `en.wav`, `en.txt`, `es.wav`, and `es.txt` in `~/me-read-compare` on the host. Each text file contains the words spoken in its WAV. Use the same WAV for both models in each language.


## Generate the samples

Run on the Radeon host:

```sh
cd ~/me-read-compare
bash run-host.sh outputs-next
```

The script gives each model exclusive GPU use. It pauses `me-read-speech` and restarts that service on exit if it was running. The run takes several minutes. Pick a new output directory for each run.

Each model loads once per language, prepares the reference, and generates one short warmup. It then reads narration five times with seed 7, followed by dialogue and pronunciation once each. The JSONL files contain timings, errors, input text, reference hashes, audio duration, peaks, and GPU memory. WAV files preserve each model's native sample rate.

Ming uses its recommended default sampling controls, speaker embeddings, and a reference transcript. `raw_text=True` sends each short test passage in one piece, following the publisher's benchmark approach. Text normalization is disabled, and numbers are written as words in both models' inputs. Chatterbox uses the same generation settings as me-read's GPU speech service.

PyTorch 2.9 changes `torchaudio.load` to use TorchCodec, whose available binary failed with this ROCm runtime. During Ming's reference preparation, `run.py` uses SoundFile to read the WAV into a float32 tensor. This restores the reader used by the publisher's PyTorch 2.6 setup. Speech generation uses the unmodified upstream model. Outputs are also written with SoundFile.

## Check the spoken words

Copy the output folder back to the Mac. From this repository, use the local Whisper cache:

```sh
scp -r radeon:me-read-compare/outputs-next artifacts/tts-compare/
server/.venv/bin/python experiments/tts-compare/analyze.py artifacts/tts-compare/outputs-next \
  --whisper /Users/julio/.local/share/me-read/models/models--mlx-community--whisper-large-v3-turbo-asr-fp16/snapshots/624c19c9af5603fa73b83bce14d4aeea96156d18
```

`analysis.json` records Whisper's transcript for each first take and a word-error estimate for narration and dialogue. Comparison ignores case and punctuation. Pronunciation passages are left unscored because ASR formats numbers and abbreviations differently from the spoken input. This estimate measures agreement with one recognizer. Listen for pronunciation, accent, pauses, expression, and resemblance to the reference before choosing a model.

Build a listening page from that analysis:

```sh
server/.venv/bin/python experiments/tts-compare/report.py artifacts/tts-compare/outputs-next \
  --references artifacts/tts-compare/references --out artifacts/tts-compare/comparison.html
```

This step needs `ffmpeg`. The page embeds both languages' recordings and has a button to reveal the models and measurements. Playback copies receive a constant volume adjustment toward -20 LUFS, limited to a true peak of -2 dB. Original WAV files stay unchanged.

The timing is synchronized generation plus transfer of the resulting samples to the CPU. It excludes imports, model loading, reference preparation, file writing, networking, and the reader pipeline. Five narration repetitions run in groups by model, rather than alternating. Treat their medians and ranges as initial measurements of these configurations on this host, not a general speed ranking or an end-to-end reader benchmark.

## Sources

- [Ming model card](https://huggingface.co/inclusionAI/Ming-3.0-tts-0.5B)
- [Ming inference code](https://github.com/inclusionAI/Ming-3.0-tts)
- [Chatterbox inference code](https://github.com/resemble-ai/chatterbox)

## Compare the 8-bit MLX conversion on the Mac

Use `mlx-community/Ming-3.0-tts-0.5B-8bit` for the Apple Silicon version of the original dense 0.5B model. `Ming-3.0-tts-tiny-8bit` is a different, larger model. The dense conversion downloads about 1.6 GB of weights.

Ming requires mlx-audio 0.6.1 or later; earlier releases have the wrong prompt contract. The first comparison kept it separate from the reader's then-pinned mlx-audio 0.5.7. me-read now uses mlx-audio 0.6.1 with Ming as its default; a new Chatterbox run below uses that current application environment, while existing results preserve the earlier versions. From this repository's uncommitted experiment checkout:

```sh
server/.venv/bin/python -m venv artifacts/tts-compare/mlx-env
artifacts/tts-compare/mlx-env/bin/pip install \
  'mlx-audio @ git+https://github.com/Blaizzy/mlx-audio.git@24547fd21503d897b52924306608ea727a9f9405' \
  'mlx==0.32.4' 'mlx-metal==0.32.4' soundfile
server/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download('mlx-community/Ming-3.0-tts-0.5B-8bit',
                  revision='d0d1da6f627e4b3f20cc2db6ba4aee86d45343ad',
                  local_dir='artifacts/tts-compare/models/ming-dense-8bit')
PY
```

Run each model separately, with no other speech generation running. Choose a fresh output folder for each comparison. The Chatterbox command uses the actual application engine and its installed dependencies; Ming uses the isolated environment.

```sh
PYTHONPATH=server server/.venv/bin/python experiments/tts-compare/run-mlx.py \
  --engine chatterbox --references artifacts/tts-compare/references \
  --out artifacts/tts-compare/mlx-next
artifacts/tts-compare/mlx-env/bin/python experiments/tts-compare/run-mlx.py \
  --engine ming --model artifacts/tts-compare/models/ming-dense-8bit \
  --references artifacts/tts-compare/references --out artifacts/tts-compare/mlx-next
server/.venv/bin/python experiments/tts-compare/analyze.py artifacts/tts-compare/mlx-next \
  --whisper /Users/julio/.local/share/me-read/models/models--mlx-community--whisper-large-v3-turbo-asr-fp16/snapshots/624c19c9af5603fa73b83bce14d4aeea96156d18
server/.venv/bin/python experiments/tts-compare/report.py artifacts/tts-compare/mlx-next \
  --runtime mlx --references artifacts/tts-compare/references \
  --out artifacts/tts-compare/mlx-next-report/comparison.html
```

The first Mac run used an Apple M1 Max with 32 GB, MLX 0.32.4 for Ming, and MLX 0.32.3 for Chatterbox. Cached Chatterbox revisions were Turbo `2f2e21a03863f86a1274d1060dcc188e7cde77e1` and Multilingual `9617d61b596a03d1bed766a28c341680e993a1b9`. These models use the application's Hugging Face cache; later runs may resolve newer revisions.

The MLX Ming port does not extract the CAM++ speaker embedding. Cloning uses the same reference audio and transcript as the earlier GPU run, without that extra conditioning. Ming also prepares the reference within every measured generation call, while Chatterbox caches its reference before timing. These differences are shown in the report. Numbers are spelled out because the MLX port does not normalize them. This experiment checks the shipped configurations; it does not isolate the effect of quantization or prove an overall speed or voice-quality winner.

- [MLX 8-bit model card](https://huggingface.co/mlx-community/Ming-3.0-tts-0.5B-8bit)
- [Pinned MLX dense implementation](https://github.com/Blaizzy/mlx-audio/tree/24547fd21503d897b52924306608ea727a9f9405/mlx_audio/tts/models/dense)

During the first Mac run, an unrelated build increased host load while Ming was running; English narration varied from 3.48 to 9.48 seconds. The listening samples are valid, but the speed comparison is inconclusive. Preserve that caveat when rebuilding this report:

```sh
server/.venv/bin/python experiments/tts-compare/report.py artifacts/tts-compare/mlx-outputs \
  --runtime mlx --references artifacts/tts-compare/references \
  --out artifacts/tts-compare/mlx-comparison/comparison.html \
  --timing-note 'Speed comparison is inconclusive: an unrelated build increased host load during the Ming run. These timings include contention and do not establish which model is faster.'
```

The first Mac comparison generated 32 WAVs including warmups and repeats, with no synthesis failures or clipping. Whisper recovered every scored narration and dialogue word for Ming in English and Spanish and for Chatterbox in Spanish. For English Chatterbox dialogue it heard “remember” where the script says “remembered” (one error across 36 scored words). These are recognizer results, not a listening verdict. Dates, currency and names remain unscored listening cases. The report preserves all 12 first takes and both reference clips.

## Compare Audio8 Air on the Radeon host

[Audio8-TTS-Realistic-Air](https://huggingface.co/Edge0/Audio8-TTS-Realistic-Air) is a 0.6B model (Apache 2.0, 44.1 kHz) that clones a voice from a reference clip and its transcript. It needs transformers below 5, so it gets its own environment that borrows the speech service's ROCm PyTorch. The setup pins the checkpoint to `23c305cc013fde378edb459f538317043c993a74`.

```sh
scp experiments/tts-compare/*.py experiments/tts-compare/*.sh radeon:me-read-compare/
ssh radeon 'cd ~/me-read-compare && bash setup-audio8-host.sh'
ssh radeon 'cd ~/me-read-compare && bash run-audio8-host.sh outputs-audio8-fp16 float16'
scp -r radeon:me-read-compare/outputs-audio8-fp16 artifacts/tts-compare/audio8/
server/.venv/bin/python experiments/tts-compare/analyze.py artifacts/tts-compare/audio8/outputs-audio8-fp16 --whisper <whisper snapshot>
server/.venv/bin/python experiments/tts-compare/report-audio8.py \
  --audio8 artifacts/tts-compare/audio8/outputs-audio8-fp16 --chatterbox artifacts/tts-compare/outputs \
  --references artifacts/tts-compare/references --audio8-label 'Audio8 Air (fp16)' \
  --out artifacts/tts-compare/audio8/comparison.html
```

The second argument of `run-audio8-host.sh` is the dtype. On this card bfloat16 runs at about 2.9 seconds of work per audio second and float16 at about 1.1, with no failures in either. The script pauses `me-read-speech` for the run and restarts it on exit.
