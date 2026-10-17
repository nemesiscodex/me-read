# How me-read works

me-read turns an EPUB into lines, finds out who says each line, gives every speaker a voice, and speaks the lines around the page you are reading. The server is in `server/meread/`, and every step below names the module that does it.

## From book to audio

1. **Import.** `epub.py` reads the EPUB into paragraphs. `segment.py` splits each paragraph into segments, the lines of the radio play. A segment is either narration or one quotation, cut to about 40 words. Joined back together, the segments of a paragraph are exactly the paragraph's text.
2. **Focus.** The reader reports which paragraphs are on the previous, current, and next page. Only those are processed, starting with the current page.
3. **Tagging.** `tagger.py` sends the paragraphs in focus, the cast so far, and some earlier text to `claude -p` or `codex exec`. The defaults are Claude Haiku 5.5 and Codex `gpt-6-luna`, and you can change them in the library's settings. The model answers who says each quotation and how: neutral, happy, sad, angry, scared, whisper, or yelling. It also describes each new character: name, gender, age, pitch, and how they sound. Unnamed speakers become extras such as "Guard 1". If tagging fails, the narrator reads the line and the paragraph is marked.
4. **Voices.** For each new character, `speech.py` designs a 15-second voice sample from the description and saves it as `books/<book>/voices/<character>.wav`. [Narrators and voice models](narrators.md#how-a-voice-is-made) describes how.
5. **Emotions.** In an English book, the first time a character speaks in an emotion other than neutral, Breeze directs their voice into a 15-second take of that emotion, saved as `books/<book>/voices/<character>.<emotion>.wav`. A take belongs to the voice it was made from. When the voice changes, each take is made again the next time a line needs it. If a take cannot be made, the line is read in the character's own voice.
6. **Speech.** The narrator clones the character's sample, or the take for the line's emotion, and reads the segment into `books/<book>/audio/<segment>.flac`.
7. **Moments.** With pictures on, `scenes.py` sends the paragraphs of the current and next page to Claude or Codex in a second call. The answer lists every moment worth a picture, with a title, a one-sentence summary, a description for the artist, and an importance from 1 to 10. It also describes how the people in each moment look.
8. **Pictures.** `images.py` asks the image service for a portrait of each character, saved as `books/<book>/portraits/<character>.png`. It then asks for the moment, with the portraits of the people in it as reference images, so a character looks the same in every picture. The moment is saved as `books/<book>/moments/<moment>.png`. The narrator is never drawn.

## Four workers and no queue

`pipeline.py` runs steps 3 to 8 in four background workers: tagging, moments, speech, and pictures. Each worker repeats one loop. It asks the database for the most useful job inside the reader's focus, does it, and records the result.

Nothing is queued in memory. A correction, a new voice, or a restart only changes what the next lookup finds. When you change a character's voice, for example, `store.py` marks their clips out of date, and the speech worker picks them up as you read.

## Pictures on the page

A picture appears above the text when the line being read reaches its paragraph, or when you turn past its page. It never appears early. It stays on later pages until the next picture. A new chapter starts without one.

On each page, the most important moments are drawn, two by default. The library's settings allow from one to twelve. A page is whatever fits the window, so the choice can change when the window does.

To have a paragraph drawn, right-click a line and choose **Draw a picture of this moment**. That costs one Claude or Codex call when the paragraph has no moment yet. A picture asked for this way has a small star. It is drawn before the others, wherever it is in the book, and does not count toward the page's limit.

In the Cast panel, you can edit how a character looks, draw the portrait again, or have their appearance looked up in the whole book. The lookup can reveal things you have not read yet.

## What is kept on disk

Everything lives in the library folder, `~/.local/share/me-read` by default:

| Path | Contents |
|---|---|
| `library.db` | Books, segments, tags, the cast, moments, settings, and the reading position (SQLite) |
| `books/<book>/voices/` | Voice samples and emotion takes |
| `books/<book>/audio/` | One FLAC clip per segment |
| `books/<book>/portraits/`, `books/<book>/moments/` | Pictures |
| `models/` | Downloaded models |
| `launcher.json` | What `./start` remembers: your consent statement and the GPU machine |

Opening a book again continues from the saved place and only makes what is missing.
