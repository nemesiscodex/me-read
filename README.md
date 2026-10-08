<img src="assets/icon.svg" width="64" height="64" alt="">

# me-read

Read an EPUB and listen to it as a radio play. The narrator and every character get their own voice, and audio is prepared in the background for the page before, the page you are on and the page after.

Everything runs on your Mac. Speaker tagging goes through your own Claude Code or Codex login; speech is generated locally.

## Run it

Needs macOS on Apple Silicon, [uv](https://docs.astral.sh/uv/), pnpm, and a logged-in `claude` or `codex` CLI.

```sh
./start
```

That builds the web app and starts the server that serves it. Arguments go to the server, for example `./start --port 9000`.

Open http://127.0.0.1:8765 and drop an EPUB on the library.

The first clip downloads the speech models (about 4 GB) into `~/.local/share/me-read/models`. Books, voices and audio live in `~/.local/share/me-read`; set `ME_READ_HOME` or pass `--home` to move them.

For frontend work, run `pnpm dev` in `web/` (port 5183) next to the server; it proxies `/api`.

## How it works

1. **Import.** `server/meread/epub.py` reads the EPUB into paragraphs. `segment.py` splits each paragraph into *segments*: narration, or one quotation, cut to about 40 words. Segments always concatenate back to the exact paragraph text.
2. **Focus.** The reader reports which paragraphs are on the previous, current and next page. Only those are processed, starting from the current page.
3. **Tagging.** `tagger.py` sends the focused paragraphs, the cast so far and some earlier text to `claude -p` (Haiku 5.5) or `codex exec` (`gpt-6-luna`). The model answers who says each quotation and describes new characters: name, gender, age, pitch and how they sound. Unnamed speakers become extras such as "Guard 1". If tagging fails, the narrator reads the line and the paragraph is flagged.
4. **Voices.** For a new character, Qwen3-TTS VoiceDesign turns the description into a 15 second sample, saved as `books/<book>/voices/<character>.wav`. In the Cast panel you can try a new voice, listen to the candidate, and only then use it; the character's lines are read again in it. You can also upload your own recording.
5. **Speech.** Chatterbox Turbo (MLX) clones that sample to read each segment into `books/<book>/audio/<segment>.flac`.

`pipeline.py` runs steps 3 to 5 as two workers that ask the database for the next most useful job, so corrections and restarts need no queue bookkeeping. Choose the tagger and its model in the library's settings.

The "Claude on" button in the reader stops and resumes the tagger and shows how many calls it made today. While it is paused the narrator reads lines that have no speaker yet, and they are read again once tagging resumes.

Everything is kept on disk: tags and the cast in `library.db`, voices and clips under `books/`. Opening a book again continues from the saved page and only generates what is missing.

## Limits

- English and Spanish. The language is detected from the text at import. Dialogue can be in quotation marks (“…”, "…", «…», British ‘…’) or introduced by dashes (—Hola —dijo Ana—. ¿Vienes?).
- English is read by Chatterbox Turbo. Spanish uses multilingual Chatterbox, which is about half as fast and still faster than playback.
- Text formatting inside paragraphs (italics, links, images) is dropped.
- One reader at a time: the server prepares the pages of the book that reported focus last.

## Tests

```sh
cd server && uv run pytest
cd web && pnpm test
```
