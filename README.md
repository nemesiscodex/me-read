<img src="assets/icon.svg" width="64" height="64" alt="">

# me-read

Read an EPUB and listen to it as a radio play. The narrator and every character get their own voice, and audio is prepared in the background for the page before, the page you are on and the page after.

Everything runs on your Mac. Speaker tagging goes through your own Claude Code or Codex login; speech is generated locally.

It can also draw key moments as courtroom sketches and show each one when the reading reaches it. That needs an image service on a second machine with a GPU.

## Run it

Needs macOS on Apple Silicon, [uv](https://docs.astral.sh/uv/), pnpm, and a logged-in `claude` or `codex` CLI.

```sh
./start
```

That builds the web app and starts the server that serves it. Arguments go to the server, for example `./start --port 9000`.

Open http://127.0.0.1:8765 and drop an EPUB on the library.

The first clip downloads the speech models (about 4 GB) into `~/.local/share/me-read/models`. Books, voices and audio live in `~/.local/share/me-read`; set `ME_READ_HOME` or pass `--home` to move them.

### Illustrations

Illustrations are off until you give the library's settings the address of an image service, for example `http://192.168.1.20:8766`. The service lives in `imager/` and runs on another machine, a Linux box with a GPU; `imager/README.md` says how to start it. Once the address is set, the reader shows a "Pictures on" switch next to the tagger switch. While it is off nothing is looked for or drawn and the page looks as it does without the service.

For frontend work, run `pnpm dev` in `web/` (port 5183) next to the server; it proxies `/api`.

## How it works

1. **Import.** `server/meread/epub.py` reads the EPUB into paragraphs. `segment.py` splits each paragraph into *segments*: narration, or one quotation, cut to about 40 words. Segments always concatenate back to the exact paragraph text.
2. **Focus.** The reader reports which paragraphs are on the previous, current and next page. Only those are processed, starting from the current page.
3. **Tagging.** `tagger.py` sends the focused paragraphs, the cast so far and some earlier text to `claude -p` (Haiku 5.5) or `codex exec` (`gpt-6-luna`). The model answers who says each quotation and describes new characters: name, gender, age, pitch and how they sound. Unnamed speakers become extras such as "Guard 1". If tagging fails, the narrator reads the line and the paragraph is flagged.
4. **Voices.** For a new character, Qwen3-TTS VoiceDesign turns the description into a 15 second sample, saved as `books/<book>/voices/<character>.wav`. The designer tends to return much the same voice for similar characters, so a sample that the speech model would take for a voice the book already has is played slightly slower or faster, by up to three semitones, until it is not. In the Cast panel you can try a new voice, listen to the candidate, and only then use it; the character's lines are read again in it. You can also upload your own recording.
5. **Speech.** Chatterbox Turbo (MLX) clones that sample to read each segment into `books/<book>/audio/<segment>.flac`.

6. **Moments.** With illustrations on, `scenes.py` sends the paragraphs of the current and next page to the same CLI in a second call. It returns every moment worth a picture, each with a title, a one-sentence summary, a detailed description of what is seen for the artist, and how important it is from 1 to 10. It can also mark where the last picture stops matching the text, and it describes how the people in a moment look.
7. **Pictures.** `images.py` asks the image service for a portrait of each character from that description, saved as `books/<book>/portraits/<character>.png`, and then for the moment itself with the portraits of the people in it as reference images, so they look the same every time: `books/<book>/moments/<moment>.png`. The narrator is never drawn.

`pipeline.py` runs steps 3 to 7 as four workers that ask the database for the next most useful job, so corrections and restarts need no queue bookkeeping. Choose the tagger and its model in the library's settings.

A picture appears above the text once the line being read reaches its paragraph, or once you turn past its page, never before. It stays there on later pages until the next one. Of the moments on a page the most important are drawn: two, or as many as the library's settings say, from one to twelve. A page is whatever fits the window, so the choice can change when the window does. A new chapter starts without a picture. Click a picture to see it full screen, with the line being read as a subtitle. To have a paragraph of your own choice drawn, right-click a line and pick "Draw a picture of this moment"; that is one Claude or Codex call when the paragraph has no moment yet. A picture asked for that way is marked with a small star, is drawn before the others wherever it is in the book, and does not count toward the page's limit. In the Cast panel you can edit how a character looks, draw the portrait again, or have the appearance looked up in the whole book, which can reveal things you have not read yet.

The "Claude on" button in the reader stops and resumes the tagger and shows how many calls it made today. While it is paused the narrator reads lines that have no speaker yet, and they are read again once tagging resumes. No moments are looked for either; pictures already found are still drawn.

Everything is kept on disk: tags, the cast and the moments in `library.db`, voices, clips and pictures under `books/`. Opening a book again continues from the saved page and only generates what is missing.

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
