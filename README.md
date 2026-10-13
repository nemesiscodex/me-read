<img src="assets/icon.svg" width="64" height="64" alt="">

# me-read

Read an EPUB and listen to it as a radio play. The narrator and every character get their own voice, and audio is prepared in the background for the page before, the page you are on and the page after.

Everything runs on your Mac. Speaker tagging goes through your own Claude Code or Codex login; speech is generated locally. If you have a Linux machine with a GPU, it can do the heavy speech work instead.

It can also draw key moments as courtroom sketches and show each one when the reading reaches it. That needs an image service on a second machine with a GPU.

## Run it

Needs macOS on Apple Silicon, [uv](https://docs.astral.sh/uv/), pnpm, and a logged-in `claude` or `codex` CLI.

```sh
./start
```

That builds the web app and starts the server that serves it. Arguments go to the server, for example `./start --port 9000`.

Open http://127.0.0.1:8765 and drop an EPUB on the library.

Models download only when needed into `~/.local/share/me-read/models`. Help does not download models. Books, voices, and audio live in `~/.local/share/me-read`; set `ME_READ_HOME` or pass `--home` to move them.

### Choose a local narrator

Run these commands from the repository root. `./start` forwards `--tts-provider` to the server. The default is `chatterbox`.

```sh
./start --tts-provider chatterbox
```

Chatterbox and Fish narrate English and Spanish, and they also accept books in other languages. To use Fish, stop the current server and run:

```sh
./start --tts-provider fish
```

Breeze supports English narration only. To try Breeze with an English book, stop the current server and run:

```sh
./start --tts-provider breeze
```

For a Spanish book, Breeze reports an error before automatically designing missing voices or loading narration models. Restart with Fish or Chatterbox to read it. There is no silent fallback to another narrator.

Without `--tts-url`, local Breeze designs English voice samples and Qwen designs Spanish ones, whichever narrator is selected. You can explicitly request, audition, and approve a Spanish candidate while Breeze is selected. That candidate can narrate after you switch to Fish or Chatterbox. Existing Chatterbox speaker comparisons keep newly designed voices distinct from the rest of the cast.

```text
Engine.design
  Breeze voice design in English, Qwen in Spanish, on this Mac or the GPU speech service
  designed again, up to four tries, while a male voice is above its pitch range
  Chatterbox speaker comparison when other voices exist, on this Mac or the service
  saved candidate or approved WAV
Engine.speak
  selected narrator's language check
  Chatterbox on the service, which clones the uploaded WAV
  or Chatterbox saved-WAV conditioning
  or local Whisper transcript of saved bytes
    Fish waveform and transcript
    or Breeze frozen WAV filename, transcript, and explicit seed
  mono audio at the model's reported sample rate
Engine.direct
  local Whisper transcript of the approved WAV
  Breeze on this Mac or the service reads the emotion's script from it, as the emotion directs
  saved take WAV, which Engine.speak then clones like any voice
Pipeline._land
  revision check
  PCM16 FLAC for the existing browser player
```

Without `--tts-url`, speech runs locally through `mlx-audio==0.5.7` on Apple Silicon using MLX. No hosted voice-design or speech service is used. These are the model IDs:

- English Chatterbox uses `mlx-community/chatterbox-turbo-8bit`.
- Spanish Chatterbox uses `mlx-community/chatterbox-8bit`.
- Chatterbox's tokenizer is `mlx-community/S3TokenizerV2`.
- Spanish voices are designed by `mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit`.
- Fish uses `mlx-community/fish-audio-s2-pro-8bit`.
- Breeze uses `mlx-community/Breeze-TTS-2-mlx-4bit`. It also designs English voices and directs emotion takes of English books, whichever narrator is selected, unless the GPU speech service does that work.
- Local transcription uses `mlx-community/whisper-large-v3-turbo-asr-fp16` at revision `624c19c9af5603fa73b83bce14d4aeea96156d18`.

Fish adds about 6.72 GB of weights. Breeze adds about 3.01 GB. Whisper adds about 1.61 GB and is shared by Fish and Breeze. Using both alternatives adds about 11.34 GB, plus the existing Qwen and Chatterbox assets. These download estimates are not peak memory requirements.

Fish and Breeze need the words actually spoken in the saved reference WAV. The reader clips references to at most 15 seconds, then transcribes those saved bytes locally. Whisper receives mono 16 kHz audio and detects the reference language rather than assuming the book's language. Its transcript is an automatic estimate, not a human-verified transcript. A full upload transcript or the full designer script is not assigned to a clipped recording. Empty or failed transcription stops the clip with an actionable error instead of guessing words. The transcription model is released after use. Content-keyed caches retain at most 24 transcripts and 24 prepared references, so an upload or approved candidate gets fresh conditioning even when the filename stays the same. Chatterbox does not use Whisper.

Fish's Fish Audio Research License restricts commercial use. Breeze's BreezeBlue Research/Non-Commercial License also restricts commercial use of self-hosted outputs. Treat these alternatives as local noncommercial experiments and review the model publishers' license terms before use.

Stop the old server before switching narrators on the same library. Concurrent servers sharing one library are unsupported. A provider or generation-identity change resets narration state, duration, and errors, and increments clip revisions. The reader regenerates those clips, including when you return to an earlier provider. Restarting with the same identity preserves ready clips. Approved WAVs, uploaded references, candidates, the cast, tags, and the exact reader position stay intact. An older library without a saved identity is treated as Chatterbox.

The mutable `provider` setting in the library selects the Claude or Codex tagger. It is separate from the startup-only `--tts-provider` narrator choice.

### Use a GPU speech service

me-read can send its heavy speech work to a speech service on a Linux machine with a GPU. The service lives in `speech-service/`. `speech-service/README.md` says how to set it up and start it on that machine.

With `--tts-url`, the service does this work:

- It designs new voices. Breeze designs voices for English books, and Qwen designs voices for Spanish books and books in other languages.
- Breeze directs the emotion takes.
- Chatterbox compares speakers, so a new voice does not sound like a voice the book already has.
- It narrates when the narrator is `chatterbox` or `breeze-gguf`. With `fish` or `breeze`, narration stays on this Mac.

The service listens on localhost only. Open a tunnel from the Mac and leave it running. Replace `HOST` with the GPU machine's SSH name:

```sh
ssh -N -L 8781:127.0.0.1:8781 HOST
```

Stop the current server. Then start me-read with the service, from the repository root:

```sh
./start --tts-url http://127.0.0.1:8781 --tts-consent "YOUR OWN STATEMENT"
```

To narrate with Breeze on the service instead of Chatterbox:

```sh
./start --tts-provider breeze-gguf --tts-url http://127.0.0.1:8781 --tts-consent "YOUR OWN STATEMENT"
```

Replace `YOUR OWN STATEMENT` with your own words. `--tts-consent` is your statement that you may use the reference voices. me-read has no default and refuses to start with `--tts-url` but without it. The text is sent with every request. `breeze-gguf` always needs the service.

- Spanish books work. Qwen designs the voices and Chatterbox Multilingual narrates. `breeze-gguf` narrates English only, so it rejects a Spanish book before it sends anything. As on the Mac, only English books get emotion takes.
- Transcripts stay on the Mac. The service needs the words spoken in each voice it receives. Whisper transcribes each saved voice and take on this Mac once, and me-read sends the transcript with the upload. With `chatterbox` and the service, Whisper is the only model that loads on the Mac.
- Clips are not read again when you move. `chatterbox` on the Mac and `chatterbox` on the service count as the same narrator, so ready clips stay ready. The service runs the full Chatterbox model, not the Mac's 8-bit copy, so new clips may sound slightly different from old ones.
- me-read asks the service to leave the spoken AI disclaimer out of every request. You are then responsible for disclosing that the audio is AI-generated. me-read refuses a cloned voice, take or clip unless the service confirms that the disclaimer was left out.
- The service handles one request at a time, so speech work waits its turn. It loads each model on first use. When the service is down, unreachable or still loading, me-read waits 15 seconds and tries again. Voices, candidates, takes and clips go back in the queue and do not fail.
- A request the service rejects fails that voice, take or clip with the service's error. Nothing falls back to this Mac.
- By default the service may use up to 13 GB of the card. On a 16 GB card the image service does not fit next to it, so run one of the two at a time.
- The text and the voices travel over plain HTTP with no login. Use the SSH tunnel, or a network you trust.
- A voice is named on the service from the SHA-256 of its exact WAV bytes. A replaced voice gets a new name, so it cannot reuse an older one. Uploads may take up to 60 seconds and speech requests up to 300 seconds.

### Illustrations

Illustrations are off until you give the library's settings the address of an image service, for example `http://192.168.1.20:8766`. The service lives in `imager/` and runs on another machine, a Linux box with a GPU; `imager/README.md` says how to start it. Once the address is set, the reader shows a "Pictures on" switch next to the tagger switch. While it is off nothing is looked for or drawn and the page looks as it does without the service.

For frontend work, run `pnpm dev` in `web/` (port 5183) next to the server; it proxies `/api`.

## How it works

1. **Import.** `server/meread/epub.py` reads the EPUB into paragraphs. `segment.py` splits each paragraph into *segments*: narration, or one quotation, cut to about 40 words. Segments always concatenate back to the exact paragraph text.
2. **Focus.** The reader reports which paragraphs are on the previous, current and next page. Only those are processed, starting from the current page.
3. **Tagging.** `tagger.py` sends the focused paragraphs, the cast so far and some earlier text to `claude -p` (Haiku 5.5) or `codex exec` (`gpt-6-luna`). The model answers who says each quotation and how it is said (neutral, happy, sad, angry, scared or whisper), and describes new characters: name, gender, age, pitch and how they sound. Unnamed speakers become extras such as "Guard 1". If tagging fails, the narrator reads the line and the paragraph is flagged.
4. **Voices.** For a new character, Breeze turns the description into a 15 second sample, saved as `books/<book>/voices/<character>.wav`. Qwen3-TTS VoiceDesign does this for books in other languages. Both run on this Mac, or on the GPU speech service when `--tts-url` is given. The designers sometimes give a man or a teenage boy a woman's voice, so a male voice whose median pitch is above the range for its pitch (160 Hz for low, 210 Hz for medium, 250 Hz for high) is designed again with the next seed, up to four tries, keeping the lowest. Teenage boys are described to the designer as young men of about eighteen for the same reason. The designer tends to return much the same voice for similar characters, so a sample that the speech model would take for a voice the book already has is played slightly slower or faster, by up to three semitones, until it is not. In the Cast panel you can try a new voice, listen to the candidate, and only then use it; the character's lines are read again in it. You can also upload your own recording.
5. **Emotions.** In an English book, the first line a character says in an emotion other than neutral has Breeze direct their voice into a 15 second *take* of that emotion, `books/<book>/voices/<character>.<emotion>.wav`. A take belongs to the voice it was made from: once the voice is replaced, each take is made again when a line needs it. Breeze directs takes on the GPU speech service when `--tts-url` is given. When a take cannot be made, the line is read in the character's own voice. Books in other languages read every line in the character's own voice. Right-click a line to change how it is said. Under each voice in the Cast panel you can hear each take, or make it again; its lines are then read again in the new take.
6. **Speech.** The selected narrator clones the saved sample, or the take for the line's emotion, to read each segment into `books/<book>/audio/<segment>.flac`. With `--tts-url`, Chatterbox and `breeze-gguf` read on the GPU speech service.

7. **Moments.** With illustrations on, `scenes.py` sends the paragraphs of the current and next page to the same CLI in a second call. It returns every moment worth a picture, each with a title, a one-sentence summary, a detailed description of what is seen for the artist, and how important it is from 1 to 10. It can also mark where the last picture stops matching the text, and it describes how the people in a moment look.
8. **Pictures.** `images.py` asks the image service for a portrait of each character from that description, saved as `books/<book>/portraits/<character>.png`, and then for the moment itself with the portraits of the people in it as reference images, so they look the same every time: `books/<book>/moments/<moment>.png`. The narrator is never drawn.

`pipeline.py` runs steps 3 to 8 as four workers that ask the database for the next most useful job, so corrections and restarts need no queue bookkeeping. Choose the tagger and its model in the library's settings.

A picture appears above the text once the line being read reaches its paragraph, or once you turn past its page, never before. It stays there on later pages until the next one. Of the moments on a page the most important are drawn: two, or as many as the library's settings say, from one to twelve. A page is whatever fits the window, so the choice can change when the window does. A new chapter starts without a picture. Click a picture to see it full screen, with the line being read as a subtitle. To have a paragraph of your own choice drawn, right-click a line and pick "Draw a picture of this moment"; that is one Claude or Codex call when the paragraph has no moment yet. A picture asked for that way is marked with a small star, is drawn before the others wherever it is in the book, and does not count toward the page's limit. In the Cast panel you can edit how a character looks, draw the portrait again, or have the appearance looked up in the whole book, which can reveal things you have not read yet.

The "Claude on" button in the reader stops and resumes the tagger and shows how many calls it made today. While it is paused the narrator reads lines that have no speaker yet, and they are read again once tagging resumes. No moments are looked for either; pictures already found are still drawn.

Everything is kept on disk: tags, the cast and the moments in `library.db`, voices, clips and pictures under `books/`. Opening a book again continues from the saved page and only generates what is missing.

## Limits

- English and Spanish. The language is detected from the text at import. Dialogue can be in quotation marks (“…”, "…", «…», British ‘…’) or introduced by dashes (—Hola —dijo Ana—. ¿Vienes?).
- Chatterbox uses Turbo for English and the multilingual model for Spanish. Fish supports both languages. Breeze narration is English-only.
- Text formatting inside paragraphs (italics, links, images) is dropped.
- One reader at a time: the server prepares the pages of the book that reported focus last.

## Tests

```sh
cd server && uv run pytest
cd web && pnpm test
```
