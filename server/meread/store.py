"""SQLite storage. The database is the single source of truth for what is tagged, voiced and spoken."""
import json
import sqlite3
import threading
from pathlib import Path

from . import epub, segment

SCHEMA = """
CREATE TABLE IF NOT EXISTS book (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, block_count INTEGER NOT NULL,
  position_block INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  language TEXT NOT NULL DEFAULT 'en');
CREATE TABLE IF NOT EXISTS chapter (
  book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE, idx INTEGER NOT NULL, title TEXT NOT NULL,
  first_block INTEGER NOT NULL, block_count INTEGER NOT NULL, PRIMARY KEY (book_id, idx));
CREATE TABLE IF NOT EXISTS block (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
  chapter INTEGER NOT NULL, tag TEXT NOT NULL CHECK (tag IN ('p', 'h')),
  tag_state TEXT NOT NULL CHECK (tag_state IN ('pending', 'tagging', 'tagged', 'failed')),
  UNIQUE (book_id, idx));
CREATE TABLE IF NOT EXISTS character (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('narrator', 'named', 'extra')),
  gender TEXT NOT NULL CHECK (gender IN ('male', 'female', 'unknown')),
  age TEXT NOT NULL CHECK (age IN ('child', 'teen', 'adult', 'elderly')),
  pitch TEXT NOT NULL CHECK (pitch IN ('low', 'medium', 'high')),
  description TEXT NOT NULL DEFAULT '',
  voice_state TEXT NOT NULL DEFAULT 'none' CHECK (voice_state IN ('none', 'designing', 'ready', 'failed')),
  voice_rev INTEGER NOT NULL DEFAULT 0, error TEXT,
  draft_state TEXT NOT NULL DEFAULT 'none' CHECK (draft_state IN ('none', 'queued', 'designing', 'ready', 'failed')),
  draft_rev INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS usage (
  day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  paragraphs INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, PRIMARY KEY (day, provider, model));
CREATE TABLE IF NOT EXISTS segment (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  block_id INTEGER NOT NULL REFERENCES block(id) ON DELETE CASCADE, block_idx INTEGER NOT NULL, seq INTEGER NOT NULL,
  text TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('narration', 'speech')),
  character_id INTEGER REFERENCES character(id),
  audio_state TEXT NOT NULL DEFAULT 'none' CHECK (audio_state IN ('none', 'generating', 'ready', 'failed')),
  audio_rev INTEGER NOT NULL DEFAULT 0, duration REAL, error TEXT);
CREATE INDEX IF NOT EXISTS segment_order ON segment (book_id, block_idx, seq);
CREATE INDEX IF NOT EXISTS segment_character ON segment (character_id);
"""
SEGMENT = 'id, block_id, seq, text, kind, character_id, audio_state, audio_rev, duration, error'
CHARACTER = ('c.id, c.book_id, c.name, c.role, c.gender, c.age, c.pitch, c.description, c.voice_state, c.voice_rev, '
             'c.error, c.draft_state, c.draft_rev, (SELECT COUNT(*) FROM segment s WHERE s.character_id = c.id AND s.kind = \'speech\') AS line_count')
FIELDS = ('name', 'gender', 'age', 'pitch', 'description')
DEFAULT_SETTINGS = {'provider': 'claude', 'claude_model': 'claude-haiku-5-5', 'codex_model': 'gpt-6-luna',
                    'tagging_paused': False}
# Columns added after the first release, for libraries created before them.
ADDED_COLUMNS = {'book': {'language': "TEXT NOT NULL DEFAULT 'en'"}, 'character': {
    'draft_state': "TEXT NOT NULL DEFAULT 'none' CHECK (draft_state IN ('none', 'queued', 'designing', 'ready', 'failed'))",
    'draft_rev': 'INTEGER NOT NULL DEFAULT 0'}}


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.home / 'library.db', check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.execute('PRAGMA journal_mode = WAL')
        for table, columns in ADDED_COLUMNS.items():
            if self.db.execute(f"SELECT 1 FROM sqlite_master WHERE name = '{table}'").fetchone():
                have = {row['name'] for row in self.db.execute(f'PRAGMA table_info({table})')}
                for column, definition in columns.items():
                    if column not in have:
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        self.db.executescript(SCHEMA)
        # Work interrupted by a restart goes back in the queue.
        self.run("UPDATE block SET tag_state = 'pending' WHERE tag_state = 'tagging'")
        self.run("UPDATE segment SET audio_state = 'none' WHERE audio_state = 'generating'")
        self.run("UPDATE character SET voice_state = 'none' WHERE voice_state = 'designing'")
        self.run("UPDATE character SET draft_state = 'queued' WHERE draft_state = 'designing'")

    def run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    # Files

    def book_dir(self, book_id):
        return self.home / 'books' / str(book_id)

    def voice_path(self, character):
        return self.book_dir(character['book_id']) / 'voices' / f"{character['id']}.wav"

    def draft_path(self, character):
        return self.book_dir(character['book_id']) / 'voices' / f"{character['id']}.draft.wav"

    def audio_path(self, book_id, segment_id):
        return self.book_dir(book_id) / 'audio' / f'{segment_id}.flac'

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
                    state = 'pending' if any(kind == 'speech' for kind, _ in pieces) else 'tagged'
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
        return self.all('SELECT id, title, author, block_count, position_block, language FROM book ORDER BY created_at DESC, id DESC')

    def book(self, book_id):
        book = self.one('SELECT id, title, author, block_count, position_block, language FROM book WHERE id = ?', (book_id,))
        if book:
            book['chapters'] = self.all('SELECT idx AS "index", title, first_block, block_count FROM chapter '
                                        'WHERE book_id = ? ORDER BY idx', (book_id,))
            book['characters'] = self.characters(book_id)
        return book

    def blocks(self, book_id, first, last):
        """Blocks with their segments for block positions first..last inclusive."""
        blocks = self.all('SELECT id, idx, chapter, tag, tag_state FROM block WHERE book_id = ? AND idx BETWEEN ? AND ? '
                          'ORDER BY idx', (book_id, first, last))
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

    def characters(self, book_id):
        return self.all(f"SELECT {CHARACTER} FROM character c WHERE c.book_id = ? "
                        "ORDER BY c.role != 'narrator', c.id", (book_id,))

    def character(self, character_id):
        return self.one(f'SELECT {CHARACTER} FROM character c WHERE c.id = ?', (character_id,))

    def narrator(self, book_id):
        return self.one(f"SELECT {CHARACTER} FROM character c WHERE c.book_id = ? AND c.role = 'narrator'", (book_id,))

    def add_character(self, book_id, name, role='named', gender='unknown', age='adult', pitch='medium', description=''):
        """Return the character with this name, creating it when the book has none."""
        with self.lock:
            found = self.one('SELECT id FROM character WHERE book_id = ? AND lower(name) = lower(?)', (book_id, name.strip()))
            if found:
                return self.character(found['id'])
            new_id = self.run('INSERT INTO character (book_id, name, role, gender, age, pitch, description) '
                              'VALUES (?, ?, ?, ?, ?, ?, ?)',
                              (book_id, name.strip(), role, gender, age, pitch, description.strip())).lastrowid
            return self.character(new_id)

    def update_character(self, character_id, changes):
        """Apply edits. The voice stays as it is until a new sample is approved."""
        fields = {k: v for k, v in changes.items() if k in FIELDS and v is not None}
        if fields:
            self.run(f"UPDATE character SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), character_id))
        return self.character(character_id)

    def request_draft(self, character_id):
        """Ask for a candidate voice sample. The current voice and its clips stay until the candidate is approved."""
        self.run("UPDATE character SET draft_state = 'queued', draft_rev = draft_rev + 1 WHERE id = ?", (character_id,))

    def discard_draft(self, character_id):
        with self.lock:
            self.run("UPDATE character SET draft_state = 'none', draft_rev = draft_rev + 1 WHERE id = ?", (character_id,))
            self.draft_path(self.character(character_id)).unlink(missing_ok=True)

    def approve_draft(self, character_id):
        """Make the candidate the character's voice and drop the clips read in the old one. False without a candidate."""
        with self.lock:
            character = self.character(character_id)
            draft = self.draft_path(character)
            if character['draft_state'] != 'ready' or not draft.exists():
                return False
            draft.replace(self.voice_path(character))
            self.reset_voice(character_id)
            self.run("UPDATE character SET voice_state = 'ready', draft_state = 'none' WHERE id = ?", (character_id,))
            return True

    def reset_voice(self, character_id):
        with self.lock:
            self.run("UPDATE character SET voice_state = 'none', voice_rev = voice_rev + 1, error = NULL WHERE id = ?",
                     (character_id,))
            self.invalidate_audio('character_id = ?', (character_id,))

    def merge_character(self, character_id, into):
        with self.lock:
            self.run('UPDATE segment SET character_id = ? WHERE character_id = ?', (into, character_id))
            self.invalidate_audio('character_id = ?', (into,))
            self.run('DELETE FROM character WHERE id = ?', (character_id,))

    # Segments

    def segment(self, segment_id):
        return self.one(f'SELECT {SEGMENT} FROM segment WHERE id = ?', (segment_id,))

    def segments_where(self, where, args=()):
        return self.all(f'SELECT {SEGMENT} FROM segment WHERE {where} ORDER BY block_idx, seq', args)

    def invalidate_audio(self, where, args=()):
        """Drop the clips of matching segments. Bumping the revision makes an in-flight clip for them land nowhere."""
        self.run(f"UPDATE segment SET audio_state = 'none', audio_rev = audio_rev + 1, duration = NULL, error = NULL "
                 f"WHERE {where}", args)

    def set_speaker(self, segment_id, character_id):
        with self.lock:
            self.run('UPDATE segment SET character_id = ? WHERE id = ?', (character_id, segment_id))
            self.invalidate_audio('id = ?', (segment_id,))
            return self.segment(segment_id)

    def retag_block(self, block_id):
        with self.lock:
            self.run("UPDATE segment SET character_id = NULL WHERE block_id = ? AND kind = 'speech'", (block_id,))
            self.invalidate_audio("block_id = ? AND kind = 'speech'", (block_id,))
            self.run("UPDATE block SET tag_state = 'pending' WHERE id = ?", (block_id,))
