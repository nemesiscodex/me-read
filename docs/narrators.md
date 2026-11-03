# Narrators and voice models

The narrator is the model that speaks every line in a character's voice. You choose it when you start me-read, with `--tts-provider`. Other models design the voices and make emotion takes.

Without `--tts-provider`, each language has its own narrator. English uses `chatterbox`. Spanish uses `audio8` when me-read is using a GPU machine's speech service, and `chatterbox` otherwise. With `--tts-provider`, that one narrator reads every language.

## Choose a narrator

| Narrator | Languages | Runs on | Notes |
|---|---|---|---|
| `chatterbox` (default for English) | English, Spanish, and other languages the model accepts | Mac or GPU machine | Clones the saved voice directly. Needs no transcript. |
| `audio8` (default for Spanish on a GPU machine) | English and Spanish | GPU machine only | Audio8 TTS Realistic Air, 0.6B, Apache 2.0. Clones the saved voice with a local transcript. About 1.1 seconds of work per second of audio on the Radeon RX 6900 XT. Needs `--tts-url` or a GPU machine with Audio8 installed. |
| `qwen-base` | English, Spanish, and other languages the model accepts | Mac | Qwen3-TTS 0.6B Base 8-bit, Apache 2.0. Clones the saved voice with a local transcript. About 0.4 to 0.6 seconds of work per second of audio on an M1 Max once loaded. Spanish emotion takes are not offered. |
| `ming` | English, Spanish, and other languages the model accepts | Mac | Ming 3.0 0.5B 8-bit. Clones the saved voice with a local transcript. Can start a clip with a short stray sound taken from the end of the saved voice. |
| `fish` | English, Spanish, and other languages the model accepts | Mac | Non-commercial licence. Adds about 6.7 GB. |
| `breeze` | English only | Mac | Non-commercial licence. |
| `breeze-gguf` | English only | GPU machine only | Needs `--tts-url` or a GPU machine. See [the GPU machine guide](gpu-machine.md). |

To change the narrator, stop the reader and start it again with the new one:

```sh
./start --tts-provider chatterbox
```

`./start` keeps using the GPU machine it remembers, if any. To keep all speech on the Mac for one start, add `--gpu local`.

To read English with Audio8 as well, start with `./start --tts-provider audio8`. To keep Spanish on Chatterbox, start with `./start --tts-provider chatterbox`.

A Breeze narrator rejects a Spanish book before it designs any voices or loads a model. Restart with `audio8`, `qwen-base`, `ming`, `chatterbox` or `fish` to read it. me-read never switches narrator on its own.

## What changes when you switch

Your cast, voices, emotion takes, tags, and reading position stay as they are. Clips made by the previous narrator are marked out of date, and the reader makes them again as you read. Switching back later makes them again too. Restarting with the same narrator keeps every clip. When only one language's narrator changes, such as Spanish moving to Audio8, only the books in that language are read again.

Run one server per library. Two servers that share a library at the same time are not supported.

The `provider` setting in the library's settings is a different thing. It picks Claude or Codex as the tagger.

## Delivery

Right-click a spoken line to choose how it is said. **Calling out** projects to someone far away. **Shouting** is a loud warning or command without anger. **Angry shouting** adds fury. **Angry (restrained)** keeps anger at a normal speaking volume.

English books use Breeze emotion takes with every narrator. Spanish books use them with Chatterbox, Audio8 or `--tts-provider ming`. Breeze receives the character's saved voice and performs an English emotion sample. The narrator uses that recording as the reference when reading Spanish lines. A take is reused for the character's lines in that emotion.

The Cast panel lets you hear and remake those takes, including for Spanish characters. The previews are in English; the book's narration stays in Spanish. The quality of the emotional delivery and voice match depends on the source recording.

The controls show only the deliveries supported by the selected narrator and book language. Fish reads Spanish from the character's normal voice without emotion takes. A changed direction invalidates its cached audio and takes, which the reader makes again as needed. When an older library first opens with Spanish emotion takes enabled, its cached Spanish lines with emotion labels are read again.

## Models

Every model downloads on first use into `~/.local/share/me-read/models`. `./start --help` downloads nothing.

| Model | Job | Size |
|---|---|---|
| `mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit` | The `qwen-base` narrator | 2.0 GB |
| `mlx-community/Ming-3.0-tts-0.5B-8bit` | The `ming` narrator | 1.6 GB |
| `mlx-community/chatterbox-turbo-8bit` | Chatterbox narration of English books | 0.7 GB |
| `mlx-community/chatterbox-8bit` | Chatterbox narration of other books | 0.9 GB |
| `mlx-community/S3TokenizerV2` | Chatterbox's tokenizer | 0.5 GB |
| `mlx-community/Breeze-TTS-2-mlx-4bit` | Designs English voices and makes emotion takes. Also the `breeze` narrator. | 3.0 GB |
| `mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit` | Designs voices for books in other languages | 3.1 GB |
| `mlx-community/whisper-large-v3-turbo-asr-fp16` | Transcribes a voice before an emotion take, and for the `audio8`, `qwen-base`, `ming`, `fish` and `breeze` narrators | 1.6 GB |
| `mlx-community/fish-audio-s2-pro-8bit` | The `fish` narrator | 6.7 GB |

`Edge0/Audio8-TTS-Realistic-Air` (2.4 GB, revision `23c305cc013fde378edb459f538317043c993a74`) is installed on the GPU machine, not downloaded by me-read. See [the speech service README](../speech-service/README.md#install-audio8). Sizes are download sizes measured from a model cache, not memory use. me-read pins Whisper to revision `624c19c9af5603fa73b83bce14d4aeea96156d18`. Ming is pinned to revision `d0d1da6f627e4b3f20cc2db6ba4aee86d45343ad`, and Qwen3-TTS Base to `50f45ef0047cde7e84c2ef04326acb8ada2436a7`. Speech runs through `mlx-audio==0.6.1`, which includes Ming’s required prompt fix.

## How a voice is made

1. The tagger describes a new character: gender, age, pitch, and how they sound.
2. Breeze turns the description into a 15-second sample for an English book. Qwen does it for other books.
3. Voice designers sometimes give a man a woman's voice. If a male voice's median pitch is above the range for its pitch (160 Hz for low, 210 Hz for medium, 250 Hz for high), me-read designs it again with the next seed, up to four tries, and keeps the lowest.
4. Designers also tend to return similar voices for similar characters. When Chatterbox's speaker model would mistake the new sample for a voice the book already has, me-read shifts its pitch, up to three semitones, until it sounds like someone else.
5. The sample is saved as `books/<book>/voices/<character>.wav`. The narrator clones it for every line.

## Transcripts

Qwen3-TTS Base, Ming, Fish and Breeze need the exact words spoken in the voice they clone. Before cloning, me-read clips the voice to at most 15 seconds and transcribes those bytes with Whisper on the Mac. Whisper detects the language itself. A transcript is an automatic estimate. If transcription fails or comes back empty, the clip fails with an error instead of guessing the words. me-read caches up to 24 transcripts, keyed by the audio content, so a replaced voice always gets a new transcript.

Chatterbox does not use transcripts. Audio8 needs the same Whisper transcript, made on the Mac and sent with each request.

The MLX Ming port clones from the recording and transcript without extracting CAM++ speaker embeddings. Its text normalization is not ported, so dates, numbers and abbreviations may be read incorrectly. Like other models, it can mispronounce names. A line that runs far beyond its text is retried with another seed.

## Licences

Fish Audio S2 Pro is under the Fish Audio Research License. Breeze TTS 2 is under the BreezeBlue Research and Non-Commercial License. Both restrict commercial use. Breeze also designs the English voices with any narrator, so treat all audio from English books as non-commercial. Read the publishers' terms before you use the audio for anything else.
