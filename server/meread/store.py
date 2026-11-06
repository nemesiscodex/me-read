"""SQLite storage. The database is the single source of truth for what is tagged, voiced and spoken."""
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import soundfile as sf

from . import epub, jobs, segment, speech
from .emotions import DIRECTIONS, EMOTIONS



def emotion_check(emotions):
    return f"CHECK (emotion IN ({', '.join(repr(e) for e in emotions)}))"


EMOTION = f"TEXT NOT NULL DEFAULT 'neutral' {emotion_check(EMOTIONS)}"
SEMITONES = 'INTEGER NOT NULL DEFAULT 0 CHECK (semitones BETWEEN -12 AND 12)'
LEVELED = 'INTEGER NOT NULL DEFAULT 0'
# A `--` comment documents the column that starts the next line. meread.web_types copies it into the web client's types.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS generation_identity (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1), identity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS book (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, block_count INTEGER NOT NULL,
  position_block INTEGER NOT NULL DEFAULT 0,
  -- With position_block, the line at the top of the page last read: segment.seq in that block.
  position_seq INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- Two-letter code detected from the text: 'en' or 'es'. It picks the speech model and the sample voices' language.
  language TEXT NOT NULL DEFAULT 'en');
CREATE TABLE IF NOT EXISTS chapter (
  book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE, idx INTEGER NOT NULL, title TEXT NOT NULL,
  first_block INTEGER NOT NULL, block_count INTEGER NOT NULL, PRIMARY KEY (book_id, idx));
CREATE TABLE IF NOT EXISTS block (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  -- Position in the whole book, zero based.
  idx INTEGER NOT NULL,
  chapter INTEGER NOT NULL, tag TEXT NOT NULL CHECK (tag IN ('p', 'h')),
  tag_state TEXT NOT NULL CHECK (tag_state IN ('pending', 'tagging', 'tagged', 'failed')),
  scene_state TEXT NOT NULL DEFAULT 'pending' CHECK (scene_state IN ('pending', 'scanning', 'done')),
  -- Who wrote the paragraph, such as a letter, when it is read in their voice. NULL otherwise.
  writer_id INTEGER REFERENCES character(id) ON DELETE SET NULL,
  UNIQUE (book_id, idx));
CREATE TABLE IF NOT EXISTS character (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('narrator', 'named', 'extra')),
  gender TEXT NOT NULL CHECK (gender IN ('male', 'female', 'unknown')),
  age TEXT NOT NULL CHECK (age IN ('child', 'teen', 'adult', 'elderly')),
  pitch TEXT NOT NULL CHECK (pitch IN ('low', 'medium', 'high')),
  description TEXT NOT NULL DEFAULT '',
  voice_state TEXT NOT NULL DEFAULT 'none' CHECK (voice_state IN ('none', 'designing', 'ready', 'failed')),
  -- Bumped whenever the 15 second sample changes; use it to bust the audio cache.
  voice_rev INTEGER NOT NULL DEFAULT 0, error TEXT,
  -- A candidate voice sample being auditioned. The current voice stays in use until the candidate is approved.
  draft_state TEXT NOT NULL DEFAULT 'none' CHECK (draft_state IN ('none', 'queued', 'designing', 'ready', 'failed')),
  draft_rev INTEGER NOT NULL DEFAULT 0,
  -- The current candidate step or failure reason.
  draft_status TEXT,
  -- When draft_status last changed, in server time (Unix seconds).
  draft_updated_at REAL,
  -- How the character looks, in words. The portrait is drawn from it, and drawn again whenever it changes.
  appearance TEXT NOT NULL DEFAULT '',
  portrait_state TEXT NOT NULL DEFAULT 'none' CHECK (portrait_state IN ('none', 'drawing', 'ready', 'failed')),
  portrait_rev INTEGER NOT NULL DEFAULT 0, portrait_error TEXT);
CREATE TABLE IF NOT EXISTS usage (
  day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  paragraphs INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, PRIMARY KEY (day, provider, model));
CREATE TABLE IF NOT EXISTS segment (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  block_id INTEGER NOT NULL REFERENCES block(id) ON DELETE CASCADE, block_idx INTEGER NOT NULL, seq INTEGER NOT NULL,
  -- Raw text. A block's segments concatenate to exactly the block's text.
  text TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('narration', 'speech')),
  -- NULL while a spoken line is still waiting for the tagger.
  character_id INTEGER REFERENCES character(id),
  audio_state TEXT NOT NULL DEFAULT 'none' CHECK (audio_state IN ('none', 'generating', 'ready', 'failed')),
  audio_rev INTEGER NOT NULL DEFAULT 0, duration REAL, error TEXT,
  -- How a spoken line is said. Narration is always neutral.
  emotion {EMOTION},
  -- 1 once the clip on disk is at the speech level all clips share. Clips made before that are leveled later, which
  -- changes the file, not audio_rev.
  leveled {LEVELED});
CREATE INDEX IF NOT EXISTS segment_order ON segment (book_id, block_idx, seq);
CREATE INDEX IF NOT EXISTS segment_unleveled ON segment (id) WHERE audio_state = 'ready' AND leveled = 0;
CREATE INDEX IF NOT EXISTS segment_character ON segment (character_id);
CREATE TABLE IF NOT EXISTS moment (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  block_id INTEGER NOT NULL UNIQUE REFERENCES block(id) ON DELETE CASCADE,
  -- block.idx of the paragraph.
  block_idx INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'picture' CHECK (kind IN ('picture', 'clear')),
  -- What the picture is drawn from. Empty for a clear.
  scene TEXT NOT NULL,
  -- A few words to put under the picture. Empty for a clear, and for a moment found before titles existed.
  title TEXT NOT NULL DEFAULT '',
  -- One sentence on what happens.
  summary TEXT NOT NULL DEFAULT '',
  -- 1 to 10. Of the moments on a page the highest count first.
  importance INTEGER NOT NULL DEFAULT 5,
  image_state TEXT NOT NULL DEFAULT 'none' CHECK (image_state IN ('none', 'drawing', 'ready', 'failed')),
  image_rev INTEGER NOT NULL DEFAULT 0, error TEXT, wanted INTEGER NOT NULL DEFAULT 0,
  -- The reader asked for this picture by hand. It is drawn wherever it is and takes none of its page's places.
  starred INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS moment_order ON moment (book_id, block_idx);
CREATE TABLE IF NOT EXISTS moment_character (
  moment_id INTEGER NOT NULL REFERENCES moment(id) ON DELETE CASCADE,
  character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE, pos INTEGER NOT NULL,
  PRIMARY KEY (moment_id, character_id));
CREATE INDEX IF NOT EXISTS moment_character_of ON moment_character (character_id);
CREATE TABLE IF NOT EXISTS voice_take (
  character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
  emotion TEXT NOT NULL {emotion_check(DIRECTIONS)},
  state TEXT NOT NULL DEFAULT 'none' CHECK (state IN ('none', 'queued', 'making', 'ready', 'failed')),
  -- Bumped each time the take is made again by hand.
  rev INTEGER NOT NULL DEFAULT 0, made_from INTEGER, error TEXT,
  -- Lines are cloned from the take moved by this many semitones, then moved back. Below 0 for a take too high to clone.
  semitones {SEMITONES}, PRIMARY KEY (character_id, emotion));
CREATE TABLE IF NOT EXISTS emotion_direction (emotion TEXT PRIMARY KEY, specification TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clip (
  key TEXT PRIMARY KEY, character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
  voice_rev INTEGER NOT NULL, language TEXT NOT NULL, text TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'queued', 'generating', 'ready', 'failed')),
  queue_pos INTEGER, duration REAL, error TEXT, requested_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS clip_queue ON clip (queue_pos) WHERE state = 'queued';
"""
# The columns of each row the API returns. meread.web_types writes the web client's types from these.
BOOK = 'id, title, author, block_count, position_block, position_seq, language'
CHAPTER = 'idx AS "index", title, first_block, block_count'
BLOCK = 'id, idx, chapter, tag, tag_state, writer_id'
CLIP = 'key, state, duration, error'
SEGMENT = 'id, block_id, seq, text, kind, character_id, emotion, audio_state, audio_rev, duration, error, leveled'
CHARACTER = ('c.id, c.book_id, c.name, c.role, c.gender, c.age, c.pitch, c.description, c.voice_state, c.voice_rev, '
             'c.error, c.draft_state, c.draft_rev, c.draft_status, c.draft_updated_at, c.appearance, c.portrait_state, c.portrait_rev, c.portrait_error, (SELECT COUNT(*) FROM segment s WHERE s.character_id = c.id AND s.kind = \'speech\') AS line_count')
REUSABLE_VOICE = ('c.id, c.book_id, c.name, c.gender, c.age, c.pitch, c.description, c.voice_rev, b.title AS book_title, '
                  'b.author AS book_author, b.language')
MOMENT = ('m.id, m.block_id, m.block_idx, m.kind, m.title, m.summary, m.scene, m.importance, m.image_state, m.image_rev, '
          'm.error, m.starred')
# A take is made from one revision of the character's voice. Once the voice is replaced, the take counts as never made.
TAKE_STATE = "CASE WHEN t.state IN ('ready', 'failed') AND t.made_from != c.voice_rev THEN 'none' ELSE t.state END"
TAKE = f't.emotion, {TAKE_STATE} AS state, t.rev, t.semitones'
# The moments a character is drawn in.
FEATURING = 'm.id IN (SELECT moment_id FROM moment_character WHERE character_id = ?)'
FIELDS = ('name', 'gender', 'age', 'pitch', 'description', 'appearance')
MAX_PICTURES_PER_PAGE = 12
DEFAULT_SETTINGS = {'provider': 'claude', 'claude_model': 'claude-haiku-5-5', 'codex_model': 'gpt-6-luna',
                    'tagging_paused': False, 'image_url': '', 'illustrations': True, 'pictures_per_page': 2}
# Columns added after the first release, for libraries created before them.
ADDED_COLUMNS = {'book': {'language': "TEXT NOT NULL DEFAULT 'en'", 'position_seq': 'INTEGER NOT NULL DEFAULT 0'}, 'character': {
    'draft_state': "TEXT NOT NULL DEFAULT 'none' CHECK (draft_state IN ('none', 'queued', 'designing', 'ready', 'failed'))",
    'draft_rev': 'INTEGER NOT NULL DEFAULT 0', 'draft_status': 'TEXT', 'draft_updated_at': 'REAL',
    'appearance': "TEXT NOT NULL DEFAULT ''",
    'portrait_state': "TEXT NOT NULL DEFAULT 'none' CHECK (portrait_state IN ('none', 'drawing', 'ready', 'failed'))",
    'portrait_rev': 'INTEGER NOT NULL DEFAULT 0', 'portrait_error': 'TEXT'},
    'block': {'scene_state': "TEXT NOT NULL DEFAULT 'pending' CHECK (scene_state IN ('pending', 'scanning', 'done'))",
              'writer_id': 'INTEGER REFERENCES character(id) ON DELETE SET NULL'},
    'moment': {'wanted': 'INTEGER NOT NULL DEFAULT 0', 'starred': 'INTEGER NOT NULL DEFAULT 0',
               'title': "TEXT NOT NULL DEFAULT ''", 'summary': "TEXT NOT NULL DEFAULT ''",
               'importance': 'INTEGER NOT NULL DEFAULT 5'},
    'segment': {'emotion': EMOTION, 'leveled': LEVELED}, 'voice_take': {'semitones': SEMITONES}}


def illustrating(settings):
    """Whether moments are looked for and drawn: there is an image service and the reader has not switched it off."""
    return bool(settings['image_url'] and settings['illustrations'])


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.home / 'library.db', check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.execute('PRAGMA journal_mode = WAL')
        added = set()
        for table, columns in ADDED_COLUMNS.items():
            if self.db.execute(f"SELECT 1 FROM sqlite_master WHERE name = '{table}'").fetchone():
                have = {row['name'] for row in self.db.execute(f'PRAGMA table_info({table})')}
                for column, definition in columns.items():
                    if column not in have:
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
                        added.add((table, column))
        self.db.executescript(SCHEMA)
        self._allow_new_emotions()
        self._reconcile_directions()
        if ('block', 'writer_id') in added:
            # Older imports assumed all unquoted prose was narration. Review it as its pages come into focus.
            self.run("UPDATE block SET tag_state = 'pending' WHERE tag = 'p'")
        if ('moment', 'importance') in added:
            # Until now only the few best moments were kept. Pages are looked through again as they are read, for
            # every moment and how much each one matters.
            self.run("UPDATE block SET scene_state = 'pending' WHERE scene_state = 'done'")
        # Work interrupted by a restart goes back in the queue.
        self.run("UPDATE block SET tag_state = 'pending' WHERE tag_state = 'tagging'")
        self.run("UPDATE block SET scene_state = 'pending' WHERE scene_state = 'scanning'")
        for job in jobs.JOBS:
            job.recover(self)

    def _allow_new_emotions(self):
        """Let emotions added after the library was made past its checks. SQLite cannot alter a CHECK, but widening
        one changes no stored row, so the table's definition is rewritten in place."""
        widened = {}
        for table, emotions in ('segment', EMOTIONS), ('voice_take', DIRECTIONS):
            sql = self.db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
            new = re.sub(r'CHECK \(emotion IN \([^)]*\)\)', emotion_check(emotions), sql)
            if new != sql:
                widened[table] = new
        if not widened:
            return
        version = self.db.execute('PRAGMA schema_version').fetchone()[0]
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute('PRAGMA writable_schema = ON')
        for table, sql in widened.items():
            self.db.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?", (sql, table))
        self.db.execute(f'PRAGMA schema_version = {version + 1}')
        self.db.execute('PRAGMA writable_schema = OFF')
        self.db.execute('COMMIT')

    def run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def _reconcile_directions(self):
        """A changed direction must replace its old takes and narration, even when the saved voice is unchanged."""
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                saved = {row['emotion']: row['specification'] for row in self.all('SELECT * FROM emotion_direction')}
                for emotion, direction in DIRECTIONS.items():
                    specification = json.dumps(direction, ensure_ascii=False)
                    if saved.get(emotion) == specification:
                        continue
                    self.run("UPDATE voice_take SET state = 'none', rev = rev + 1, error = NULL WHERE emotion = ?", (emotion,))
                    self.invalidate_audio('emotion = ?', (emotion,))
                    self.run('INSERT INTO emotion_direction VALUES (?, ?) ON CONFLICT (emotion) '
                             'DO UPDATE SET specification = excluded.specification', (emotion, specification))
                if self.one('PRAGMA user_version')['user_version'] < 1:
                    # Spanish emotion labels previously played neutral clips or used direct Fish prompts.
                    self.invalidate_audio("book_id IN (SELECT id FROM book WHERE language = 'es') AND emotion != 'neutral'")
                    self.run('PRAGMA user_version = 1')
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def reconcile_generation(self, identity, legacy_identity):
        """Select the library's narrators before workers start, invalidating only the clips of languages whose
        narrator identity changed. An identity is one narrator's for every language, or {'narrators': {language: ...}}."""
        def by_language(value):
            return value['narrators'] if 'narrators' in value else {language: value for language in speech.LANGUAGES}
        encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'))
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                saved = self.one('SELECT identity FROM generation_identity WHERE singleton = 1')
                before, after = by_language(json.loads(saved['identity']) if saved else legacy_identity), by_language(identity)
                changed = [language for language in after if before.get(language) != after[language]]
                if len(changed) == len(after):
                    self.invalidate_audio('1')
                elif changed:
                    self.invalidate_audio(f"book_id IN (SELECT id FROM book WHERE language IN ({','.join('?' * len(changed))}))",
                                          changed)
                self.db.execute('INSERT INTO generation_identity (singleton, identity) VALUES (1, ?) '
                                'ON CONFLICT (singleton) DO UPDATE SET identity = excluded.identity', (encoded,))
                self.db.execute('COMMIT')
                return bool(changed)
            except Exception:
                self.db.execute('ROLLBACK')
                raise

    # Files

    def book_dir(self, book_id):
        return self.home / 'books' / str(book_id)

    def voice_path(self, character):
        return self.book_dir(character['book_id']) / 'voices' / f"{character['id']}.wav"

    def draft_path(self, character):
        return self.book_dir(character['book_id']) / 'voices' / f"{character['id']}.draft.wav"

    def take_path(self, character, emotion):
        return self.book_dir(character['book_id']) / 'voices' / f"{character['id']}.{emotion}.wav"

    def audio_path(self, book_id, segment_id):
        return self.book_dir(book_id) / 'audio' / f'{segment_id}.flac'

    def portrait_path(self, character):
        return self.book_dir(character['book_id']) / 'portraits' / f"{character['id']}.png"

    def moment_path(self, book_id, moment_id):
        return self.book_dir(book_id) / 'moments' / f'{moment_id}.png'

    def clip_path(self, key):
        return self.home / 'clips' / f'{key}.flac'

    # Settings

    def settings(self):
        path = self.home / 'settings.json'
        saved = json.loads(path.read_text()) if path.exists() else {}
        return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}

    def update_settings(self, changes):
        merged = {**self.settings(), **changes}
        (self.home / 'settings.json').write_text(json.dumps(merged, indent=1))
        return merged

    # Tagger usage

    def record_usage(self, provider, model, paragraphs, cost_usd):
        self.run("INSERT INTO usage (day, provider, model, calls, paragraphs, cost_usd) VALUES (date('now', 'localtime'), ?, ?, 1, ?, ?) "
                 'ON CONFLICT (day, provider, model) DO UPDATE SET calls = calls + 1, paragraphs = paragraphs + excluded.paragraphs, '
                 'cost_usd = cost_usd + excluded.cost_usd', (provider, model, paragraphs, cost_usd or 0))

    def usage(self):
        totals = 'COALESCE(SUM(calls), 0) AS calls, COALESCE(SUM(paragraphs), 0) AS paragraphs, COALESCE(SUM(cost_usd), 0) AS cost_usd'
        return {'today': self.one(f"SELECT {totals} FROM usage WHERE day = date('now', 'localtime')"),
                'all_time': self.one(f'SELECT {totals} FROM usage')}

    # Books

    def add_book(self, parsed: epub.Book):
        style = segment.quote_style([text for c in parsed.chapters for _, text in c.blocks])
        with self.lock:
            self.db.execute('BEGIN')
            total = sum(len(c.blocks) for c in parsed.chapters)
            book_id = self.db.execute('INSERT INTO book (title, author, block_count, language) VALUES (?, ?, ?, ?)',
                                      (parsed.title, parsed.author, total, parsed.language)).lastrowid
            narrator = self.db.execute(
                "INSERT INTO character (book_id, name, role, gender, age, pitch, description) "
                "VALUES (?, 'Narrator', 'narrator', 'unknown', 'adult', 'medium', ?)",
                (book_id, 'warm, clear storyteller, calm and expressive')).lastrowid
            idx = 0
            for n, chapter in enumerate(parsed.chapters):
                self.db.execute('INSERT INTO chapter VALUES (?, ?, ?, ?, ?)',
                                (book_id, n, chapter.title, idx, len(chapter.blocks)))
                for tag, text in chapter.blocks:
                    pieces = segment.segments(text, tag, style)
                    state = 'pending' if tag == 'p' else 'tagged'
                    block_id = self.db.execute(
                        'INSERT INTO block (book_id, idx, chapter, tag, tag_state) VALUES (?, ?, ?, ?, ?)',
                        (book_id, idx, n, tag, state)).lastrowid
                    self.db.executemany(
                        'INSERT INTO segment (book_id, block_id, block_idx, seq, text, kind, character_id) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        [(book_id, block_id, idx, seq, piece, kind, narrator if kind == 'narration' else None)
                         for seq, (kind, piece) in enumerate(pieces)])
                    idx += 1
            self.db.execute('COMMIT')
        return book_id

    def books(self):
        return self.all(f'SELECT {BOOK} FROM book ORDER BY created_at DESC, id DESC')

    def summary(self, book_id):
        return self.one(f'SELECT {BOOK} FROM book WHERE id = ?', (book_id,))

    def book(self, book_id):
        book = self.summary(book_id)
        if book:
            book['chapters'] = self.all(f'SELECT {CHAPTER} FROM chapter WHERE book_id = ? ORDER BY idx', (book_id,))
            book['characters'] = self.characters(book_id)
        return book

    def language(self, book_id):
        return self.one('SELECT language FROM book WHERE id = ?', (book_id,))['language']

    def delete_book(self, book_id):
        """Remove a book with its files. Clips read aloud for other apps keep their files."""
        with self.lock:
            self.run('DELETE FROM segment WHERE book_id = ?', (book_id,))
            self.run('DELETE FROM book WHERE id = ?', (book_id,))
        shutil.rmtree(self.book_dir(book_id), ignore_errors=True)

    def block(self, block_id):
        """Where a paragraph is: its id, book and position."""
        return self.one('SELECT id, book_id, idx FROM block WHERE id = ?', (block_id,))

    def blocks(self, book_id, first, last):
        """Blocks with their segments for block positions first..last inclusive."""
        blocks = self.all(f'SELECT {BLOCK} FROM block WHERE book_id = ? AND idx BETWEEN ? AND ? ORDER BY idx',
                          (book_id, first, last))
        by_id = {b['id']: b for b in blocks}
        for b in blocks:
            b['segments'] = []
        for s in self.all(f'SELECT {SEGMENT} FROM segment WHERE book_id = ? AND block_idx BETWEEN ? AND ? '
                          'ORDER BY block_idx, seq', (book_id, first, last)):
            by_id[s['block_id']]['segments'].append(s)
        return blocks

    def chapter_blocks(self, book_id, chapter):
        row = self.one('SELECT first_block, block_count FROM chapter WHERE book_id = ? AND idx = ?', (book_id, chapter))
        return None if row is None else self.blocks(book_id, row['first_block'], row['first_block'] + row['block_count'] - 1)

    # Characters

    def characters_where(self, where, args=()):
        """Characters with their takes, one for each emotion other than neutral."""
        with self.lock:
            characters = self.all(f"SELECT {CHARACTER} FROM character c WHERE {where} ORDER BY c.role != 'narrator', c.id", args)
            ids = [c['id'] for c in characters]
            made = {(t.pop('character_id'), t['emotion']): t for t in self.all(
                f'SELECT t.character_id, {TAKE} FROM voice_take t '
                f"JOIN character c ON c.id = t.character_id WHERE t.character_id IN ({','.join('?' * len(ids))})", ids)}
        for c in characters:
            c['takes'] = [made.get((c['id'], emotion), {'emotion': emotion, 'state': 'none', 'rev': 0, 'semitones': 0})
                          for emotion in DIRECTIONS]
        return characters

    def characters(self, book_id):
        return self.characters_where('c.book_id = ?', (book_id,))

    def reusable_voices(self):
        with self.lock:
            voices = self.all(
                f"SELECT {REUSABLE_VOICE} FROM character c JOIN book b ON b.id = c.book_id WHERE c.voice_state = 'ready' "
                'ORDER BY c.name COLLATE NOCASE, b.title COLLATE NOCASE, c.id')
            return [voice for voice in voices if self.voice_path(voice).is_file()]

    def character(self, character_id):
        return next(iter(self.characters_where('c.id = ?', (character_id,))), None)

    def narrator(self, book_id):
        return next(iter(self.characters_where("c.book_id = ? AND c.role = 'narrator'", (book_id,))), None)

    def add_character(self, book_id, name, role='named', gender='unknown', age='adult', pitch='medium', description='',
                      appearance=''):
        """Return the character with this name, creating it when the book has none."""
        with self.lock:
            found = self.one('SELECT id FROM character WHERE book_id = ? AND lower(name) = lower(?)', (book_id, name.strip()))
            if found:
                return self.character(found['id'])
            new_id = self.run('INSERT INTO character (book_id, name, role, gender, age, pitch, description, appearance) '
                              'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (book_id, name.strip(), role, gender, age, pitch, description.strip(), appearance.strip())).lastrowid
            return self.character(new_id)

    def update_character(self, character_id, changes):
        """Apply edits. The voice stays as it is until a new sample is approved; a new appearance is drawn at once."""
        fields = {k: v for k, v in changes.items() if k in FIELDS and v is not None}
        with self.lock:
            before = self.character(character_id)
            if fields:
                self.run(f"UPDATE character SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), character_id))
            if fields.get('appearance', before['appearance']) != before['appearance']:
                self.reset_portrait(character_id)
            return self.character(character_id)

    def reset_portrait(self, character_id):
        """Drop the portrait and the pictures it was a reference for. The new revisions make drawings in flight land nowhere."""
        with self.lock:
            self.run("UPDATE character SET portrait_state = 'none', portrait_rev = portrait_rev + 1, portrait_error = NULL "
                     'WHERE id = ?', (character_id,))
            self.redraw_moments(FEATURING, (character_id,))

    def request_draft(self, character_id):
        """Ask for a candidate voice sample. The current voice and its clips stay until the candidate is approved."""
        self.run("UPDATE character SET draft_state = 'queued', draft_rev = draft_rev + 1, "
                 "draft_status = 'Waiting for the speech worker…', draft_updated_at = ? WHERE id = ?", (time.time(), character_id))

    def discard_draft(self, character_id):
        with self.lock:
            self.run("UPDATE character SET draft_state = 'none', draft_rev = draft_rev + 1, "
                     "draft_status = NULL, draft_updated_at = NULL WHERE id = ?", (character_id,))
            self.draft_path(self.character(character_id)).unlink(missing_ok=True)

    def approve_draft(self, character_id):
        """Make the candidate the character's voice. False without a candidate."""
        with self.lock:
            character = self.character(character_id)
            draft = self.draft_path(character)
            if character['draft_state'] != 'ready' or not draft.exists():
                return False
            self.replace_voice(character_id, draft.read_bytes())
            draft.unlink()
            self.run("UPDATE character SET draft_state = 'none', draft_status = NULL, draft_updated_at = NULL WHERE id = ?",
                     (character_id,))
            return True

    def replace_voice(self, character_id, wav):
        """Make the WAV bytes the character's voice and drop the takes, lines and clips made in the old one. The file
        moves into place whole, so the speech worker and the browser never read half of it."""
        def ready():
            self.reset_voice(character_id)
            return self.run("UPDATE character SET voice_state = 'ready' WHERE id = ?", (character_id,)).rowcount
        jobs.land(self, lambda path: Path(path).write_bytes(wav), self.voice_path(self.character(character_id)), ready)

    def reset_voice(self, character_id):
        with self.lock:
            self.run("UPDATE character SET voice_state = 'none', voice_rev = voice_rev + 1, error = NULL WHERE id = ?",
                     (character_id,))
            self.run("UPDATE voice_take SET state = 'none', rev = rev + 1, made_from = NULL, error = NULL "
                     'WHERE character_id = ?', (character_id,))
            self.invalidate_audio('character_id = ?', (character_id,))
            # A clip's key names the old voice, so no request asks for it again, and one in flight lands nowhere.
            self.run("UPDATE clip SET state = 'idle' WHERE character_id = ? AND state IN ('queued', 'generating')",
                     (character_id,))

    def reuse_voice(self, character_id, source_id, source_rev):
        """Copy a saved voice independently of its book. Refuse a sample changed since it was offered."""
        with self.lock:
            source = self.character(source_id)
            if source is None or source['voice_state'] != 'ready' or source['voice_rev'] != source_rev:
                return False
            path = self.voice_path(source)
            if not path.is_file():
                return False
            self.replace_voice(character_id, path.read_bytes())
            self.discard_draft(character_id)
            return True

    def shift_draft(self, character_id, semitones):
        """Offer the current voice moved by `semitones` as the candidate, replacing one being designed. False without a voice."""
        with self.lock:
            character = self.character(character_id)
            voice = self.voice_path(character)
            if character['voice_state'] != 'ready' or not voice.is_file():
                return False
            audio, rate = sf.read(voice, dtype='float32', always_2d=True)
            target = self.draft_path(character)
            with tempfile.TemporaryDirectory(dir=target.parent) as folder:
                sample = Path(folder) / 'draft.wav'
                sf.write(sample, speech.shifted(audio.mean(axis=1), semitones), rate, format='WAV')
                sample.replace(target)
            self.run("UPDATE character SET draft_state = 'ready', draft_rev = draft_rev + 1, draft_status = NULL, "
                     'draft_updated_at = NULL WHERE id = ?', (character_id,))
            return True

    def take(self, character_id, emotion):
        return next(t for t in self.character(character_id)['takes'] if t['emotion'] == emotion)

    def request_take(self, character_id, emotion):
        """Have a take made now, or made again. Once it is ready, the lines read in it are read again."""
        self.run("INSERT INTO voice_take (character_id, emotion, state) VALUES (?, ?, 'queued') "
                 "ON CONFLICT (character_id, emotion) DO UPDATE SET state = 'queued', rev = rev + 1, error = NULL",
                 (character_id, emotion))

    def shift_take(self, character_id, emotion, semitones):
        """Have the lines in a take cloned from it moved by `semitones`, then moved back. When that changes, the lines
        already read in it are read again. Returns whether it changed."""
        with self.lock:
            if self.take(character_id, emotion)['semitones'] == semitones:
                return False
            self.run('INSERT INTO voice_take (character_id, emotion, semitones) VALUES (?, ?, ?) '
                     'ON CONFLICT (character_id, emotion) DO UPDATE SET semitones = excluded.semitones',
                     (character_id, emotion, semitones))
            self.invalidate_audio("character_id = ? AND emotion = ? AND kind = 'speech'", (character_id, emotion))
            return True

    def merge_character(self, character_id, into):
        """Give a character's lines, writings and pictures to `into`, then delete it with its voice, takes and portrait."""
        with self.lock:
            character = self.character(character_id)
            self.run('UPDATE block SET writer_id = ? WHERE writer_id = ?', (into, character_id))
            self.run('UPDATE segment SET character_id = ? WHERE character_id = ?', (into, character_id))
            self.invalidate_audio('character_id = ?', (into,))
            # A moment that shows both keeps the survivor's row; the other goes with the deleted character.
            self.run('UPDATE OR IGNORE moment_character SET character_id = ? WHERE character_id = ?', (into, character_id))
            self.run('DELETE FROM character WHERE id = ?', (character_id,))
        for path in (self.voice_path(character), self.draft_path(character), self.portrait_path(character),
                     *(self.take_path(character, emotion) for emotion in DIRECTIONS)):
            path.unlink(missing_ok=True)

    # Segments

    def segment(self, segment_id):
        return self.one(f'SELECT {SEGMENT} FROM segment WHERE id = ?', (segment_id,))

    def segments_where(self, where, args=()):
        return self.all(f'SELECT {SEGMENT} FROM segment WHERE {where} ORDER BY block_idx, seq', args)

    def invalidate_audio(self, where, args=()):
        """Drop the clips of matching segments. Bumping the revision makes an in-flight clip for them land nowhere."""
        self.run(f"UPDATE segment SET audio_state = 'none', audio_rev = audio_rev + 1, duration = NULL, error = NULL "
                 f"WHERE {where}", args)

    def correct_line(self, segment_id, character_id=None, emotion=None):
        """Change who says a line or how, and have it read again."""
        with self.lock:
            self.run('UPDATE segment SET character_id = COALESCE(?, character_id), emotion = COALESCE(?, emotion) '
                     'WHERE id = ?', (character_id, emotion, segment_id))
            self.invalidate_audio('id = ?', (segment_id,))
            return self.segment(segment_id)

    def retag_blocks(self, where, args=()):
        """Clear the speakers and emotions of matching paragraphs and send them back to the tagger. Returns their ids."""
        with self.lock:
            ids = [row['id'] for row in self.all(f'SELECT id FROM block WHERE {where}', args)]
            if ids:
                marks = ','.join('?' * len(ids))
                self.run(f"UPDATE segment SET character_id = CASE WHEN kind = 'speech' THEN NULL ELSE "
                         f"(SELECT id FROM character WHERE book_id = segment.book_id AND role = 'narrator') END, "
                         f"emotion = 'neutral' WHERE block_id IN ({marks})", ids)
                self.invalidate_audio(f"block_id IN ({marks})", ids)
                self.run(f"UPDATE block SET tag_state = 'pending', writer_id = NULL WHERE id IN ({marks})", ids)
            return ids

    def set_writer(self, block_id, character):
        """Read an entire paragraph in one voice, cancelling any attribution request in flight."""
        with self.lock:
            self.run("UPDATE block SET writer_id = ?, tag_state = 'tagged' WHERE id = ?",
                     (None if character['role'] == 'narrator' else character['id'], block_id))
            self.run("UPDATE segment SET character_id = ?, emotion = 'neutral' WHERE block_id = ?",
                     (character['id'], block_id))
            self.invalidate_audio('block_id = ?', (block_id,))

    # Moments

    def moments_where(self, where, args=()):
        moments = self.all(f'SELECT {MOMENT} FROM moment m WHERE {where} ORDER BY m.block_idx', args)
        for m in moments:
            m['starred'] = bool(m['starred'])
            m['character_ids'] = [row['character_id'] for row in self.all(
                'SELECT character_id FROM moment_character WHERE moment_id = ? ORDER BY pos', (m['id'],))]
        return moments

    def moment(self, moment_id):
        found = self.moments_where('m.id = ?', (moment_id,))
        return found[0] if found else None

    def chapter_moments(self, book_id, chapter):
        row = self.one('SELECT first_block, block_count FROM chapter WHERE book_id = ? AND idx = ?', (book_id, chapter))
        return None if row is None else self.moments_where(
            'm.book_id = ? AND m.block_idx BETWEEN ? AND ?', (book_id, row['first_block'], row['first_block'] + row['block_count'] - 1))

    def redraw_moments(self, where, args=()):
        self.run("UPDATE moment AS m SET image_state = 'none', image_rev = image_rev + 1, error = NULL "
                 f"WHERE m.kind = 'picture' AND {where}", args)

    def redraw_moment(self, moment_id):
        """Draw a picture again. Asking by hand also asks for one the page cap or a quick page turn left undrawn."""
        with self.lock:
            self.run('UPDATE moment SET wanted = 1 WHERE id = ?', (moment_id,))
            self.redraw_moments('m.id = ?', (moment_id,))

    # Clips read aloud for other apps

    def clip(self, key):
        return self.one(f'SELECT {CLIP} FROM clip WHERE key = ?', (key,))

    def request_clips(self, character, language, identity, texts):
        """Queue the clips of `texts` in their order, making the ones not seen before, and take every other clip out of
        the queue, the way a new focus replaces the old one. Clips ready, being read or failed stay as they are.
        Returns the clips in the order of `texts`."""
        keys = [clip_key(character, identity, text) for text in texts]
        marks, now = ','.join('?' * len(keys)), time.time()
        with self.lock:
            self.db.executemany('INSERT OR IGNORE INTO clip (key, character_id, voice_rev, language, text, requested_at) '
                                'VALUES (?, ?, ?, ?, ?, ?)',
                                [(key, character['id'], character['voice_rev'], language, text, now)
                                 for key, text in zip(keys, texts)])
            self.run(f'UPDATE clip SET requested_at = ? WHERE key IN ({marks})', (now, *keys))
            self.run(f"UPDATE clip SET state = 'idle' WHERE state = 'queued' AND key NOT IN ({marks})", keys)
            self.db.executemany("UPDATE clip SET state = 'queued', queue_pos = ? WHERE key = ? AND state IN ('idle', 'queued')",
                                list(enumerate(dict.fromkeys(keys))))
            found = {clip['key']: clip for clip in self.all(f'SELECT {CLIP} FROM clip WHERE key IN ({marks})', keys)}
        return [found[key] for key in keys]


def clip_key(character, identity, text):
    """The address of a clip, made from who reads it, the revision of their voice, the narrator, and the exact words. A
    new voice or narrator gives new addresses, so a ready clip never goes out of date."""
    named = json.dumps([character['id'], character['voice_rev'], identity, text], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(named.encode()).hexdigest()[:32]
