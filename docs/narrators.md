# Narrators and voice models

The narrator is the model that speaks every line in a character's voice. You choose it when you start me-read, with `--tts-provider`. Other models design the voices and make the emotion takes, whichever narrator you choose.

## Choose a narrator

| Narrator | Languages | Runs on | Notes |
|---|---|---|---|
| `chatterbox` (default) | English, Spanish, and other languages the model accepts | Mac or GPU machine | Clones the saved voice directly. Needs no transcript. |
| `fish` | English, Spanish, and other languages the model accepts | Mac | Non-commercial licence. Adds about 6.7 GB. |
| `breeze` | English only | Mac | Non-commercial licence. |
| `breeze-gguf` | English only | GPU machine only | Needs `--tts-url` or a GPU machine. See [the GPU machine guide](gpu-machine.md). |

To change the narrator, stop the reader and start it again with the new one:

```sh
./start --tts-provider fish
```

`./start` keeps using the GPU machine it remembers, if any. To keep all speech on the Mac for one start, add `--gpu local`.

A Breeze narrator rejects a Spanish book before it designs any voices or loads a model. Restart with `chatterbox` or `fish` to read it. me-read never switches narrator on its own.

## What changes when you switch

Your cast, voices, emotion takes, tags, and reading position stay as they are. Clips made by the previous narrator are marked out of date, and the reader makes them again as you read. Switching back later makes them again too. Restarting with the same narrator keeps every clip.

Run one server per library. Two servers that share a library at the same time are not supported.

The `provider` setting in the library's settings is a different thing. It picks Claude or Codex as the tagger.

## Models

Every model downloads on first use into `~/.local/share/me-read/models`. `./start --help` downloads nothing.

| Model | Job | Size |
|---|---|---|
| `mlx-community/chatterbox-turbo-8bit` | Chatterbox narration of English books | 0.7 GB |
| `mlx-community/chatterbox-8bit` | Chatterbox narration of other books | 0.9 GB |
| `mlx-community/S3TokenizerV2` | Chatterbox's tokenizer | 0.5 GB |
| `mlx-community/Breeze-TTS-2-mlx-4bit` | Designs English voices and makes emotion takes. Also the `breeze` narrator. | 3.0 GB |
| `mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit` | Designs voices for books in other languages | 3.1 GB |
| `mlx-community/whisper-large-v3-turbo-asr-fp16` | Transcribes a voice before an emotion take, and for the `fish` and `breeze` narrators | 1.6 GB |
| `mlx-community/fish-audio-s2-pro-8bit` | The `fish` narrator | 6.7 GB |

Sizes are download sizes measured from a model cache, not memory use. me-read pins Whisper to revision `624c19c9af5603fa73b83bce14d4aeea96156d18`. Speech runs through `mlx-audio==0.5.7`.

## How a voice is made

1. The tagger describes a new character: gender, age, pitch, and how they sound.
2. Breeze turns the description into a 15-second sample for an English book. Qwen does it for other books.
3. Voice designers sometimes give a man a woman's voice. If a male voice's median pitch is above the range for its pitch (160 Hz for low, 210 Hz for medium, 250 Hz for high), me-read designs it again with the next seed, up to four tries, and keeps the lowest.
4. Designers also tend to return similar voices for similar characters. When Chatterbox's speaker model would mistake the new sample for a voice the book already has, me-read shifts its pitch, up to three semitones, until it sounds like someone else.
5. The sample is saved as `books/<book>/voices/<character>.wav`. The narrator clones it for every line.

## Transcripts

Fish and Breeze need the exact words spoken in the voice they clone. Before cloning, me-read clips the voice to at most 15 seconds and transcribes those bytes with Whisper on the Mac. Whisper detects the language itself. A transcript is an automatic estimate. If transcription fails or comes back empty, the clip fails with an error instead of guessing the words. me-read caches up to 24 transcripts, keyed by the audio content, so a replaced voice always gets a new transcript.

Chatterbox does not use transcripts.

## Licences

Fish Audio S2 Pro is under the Fish Audio Research License. Breeze TTS 2 is under the BreezeBlue Research and Non-Commercial License. Both restrict commercial use. Breeze also designs the English voices with any narrator, so treat all audio from English books as non-commercial. Read the publishers' terms before you use the audio for anything else.
