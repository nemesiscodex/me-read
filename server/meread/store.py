"""SQLite storage. The database is the single source of truth for what is tagged, voiced and spoken."""
import json
import re
import sqlite3
import threading
from pathlib import Path

from . import epub, segment
from .emotions import DIRECTIONS, EMOTIONS



def emotion_check(emotions):
    return f"CHECK (emotion IN ({', '.join(repr(e) for e in emotions)}))"


EMOTION = f"TEXT NOT NULL DEFAULT 'neutral' {emotion_check(EMOTIONS)}"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS generation_identity (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1), identity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS book (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, block_count INTEGER NOT NULL,
  position_block INTEGER NOT NULL DEFAULT 0, position_seq INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  language TEXT NOT NULL DEFAULT 'en');
CREATE TABLE IF NOT EXISTS chapter (
  book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE, idx INTEGER NOT NULL, title TEXT NOT NULL,
  first_block INTEGER NOT NULL, block_count INTEGER NOT NULL, PRIMARY KEY (book_id, idx));
CREATE TABLE IF NOT EXISTS block (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
  chapter INTEGER NOT NULL, tag TEXT NOT NULL CHECK (tag IN ('p', 'h')),
  tag_state TEXT NOT NULL CHECK (tag_state IN ('pending', 'tagging', 'tagged', 'failed')),
  scene_state TEXT NOT NULL DEFAULT 'pending' CHECK (scene_state IN ('pending', 'scanning', 'done')),
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
  draft_rev INTEGER NOT NULL DEFAULT 0, appearance TEXT NOT NULL DEFAULT '',
  portrait_state TEXT NOT NULL DEFAULT 'none' CHECK (portrait_state IN ('none', 'drawing', 'ready', 'failed')),
  portrait_rev INTEGER NOT NULL DEFAULT 0, portrait_error TEXT);
CREATE TABLE IF NOT EXISTS usage (
  day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  paragraphs INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, PRIMARY KEY (day, provider, model));
CREATE TABLE IF NOT EXISTS segment (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  block_id INTEGER NOT NULL REFERENCES block(id) ON DELETE CASCADE, block_idx INTEGER NOT NULL, seq INTEGER NOT NULL,
  text TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('narration', 'speech')),
  character_id INTEGER REFERENCES character(id),
  audio_state TEXT NOT NULL DEFAULT 'none' CHECK (audio_state IN ('none', 'generating', 'ready', 'failed')),
  audio_rev INTEGER NOT NULL DEFAULT 0, duration REAL, error TEXT, emotion {EMOTION});
CREATE INDEX IF NOT EXISTS segment_order ON segment (book_id, block_idx, seq);
CREATE INDEX IF NOT EXISTS segment_character ON segment (character_id);
CREATE TABLE IF NOT EXISTS moment (
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  block_id INTEGER NOT NULL UNIQUE REFERENCES block(id) ON DELETE CASCADE, block_idx INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'picture' CHECK (kind IN ('picture', 'clear')), scene TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', importance INTEGER NOT NULL DEFAULT 5,
  image_state TEXT NOT NULL DEFAULT 'none' CHECK (image_state IN ('none', 'drawing', 'ready', 'failed')),
  image_rev INTEGER NOT NULL DEFAULT 0, error TEXT, wanted INTEGER NOT NULL DEFAULT 0,
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
  rev INTEGER NOT NULL DEFAULT 0, made_from INTEGER, error TEXT, PRIMARY KEY (character_id, emotion));
"""
SEGMENT = 'id, block_id, seq, text, kind, character_id, emotion, audio_state, audio_rev, duration, error'
CHARACTER = ('c.id, c.book_id, c.name, c.role, c.gender, c.age, c.pitch, c.description, c.voice_state, c.voice_rev, '
             'c.error, c.draft_state, c.draft_rev, c.appearance, c.portrait_state, c.portrait_rev, c.portrait_error, (SELECT COUNT(*) FROM segment s WHERE s.character_id = c.id AND s.kind = \'speech\') AS line_count')
MOMENT = ('m.id, m.block_id, m.block_idx, m.kind, m.title, m.summary, m.scene, m.importance, m.image_state, m.image_rev, '
          'm.error, m.starred')
# A take is made from one revision of the character's voice. Once the voice is replaced, the take counts as never made.
TAKE_STATE = "CASE WHEN t.state IN ('ready', 'failed') AND t.made_from != c.voice_rev THEN 'none' ELSE t.state END"
# The moments a character is drawn in.
FEATURING = 'm.id IN (SELECT moment_id FROM moment_character WHERE character_id = ?)'
FIELDS = ('name', 'gender', 'age', 'pitch', 'description', 'appearance')
MAX_PICTURES_PER_PAGE = 12
DEFAULT_SETTINGS = {'provider': 'claude', 'claude_model': 'claude-haiku-5-5', 'codex_model': 'gpt-6-luna',
                    'tagging_paused': False, 'image_url': '', 'illustrations': True, 'pictures_per_page': 2}
# Columns added after the first release, for libraries created before them.
ADDED_COLUMNS = {'book': {'language': "TEXT NOT NULL DEFAULT 'en'", 'position_seq': 'INTEGER NOT NULL DEFAULT 0'}, 'character': {
    'draft_state': "TEXT NOT NULL DEFAULT 'none' CHECK (draft_state IN ('none', 'queued', 'designing', 'ready', 'failed'))",
    'draft_rev': 'INTEGER NOT NULL DEFAULT 0', 'appearance': "TEXT NOT NULL DEFAULT ''",
    'portrait_state': "TEXT NOT NULL DEFAULT 'none' CHECK (portrait_state IN ('none', 'drawing', 'ready', 'failed'))",
    'portrait_rev': 'INTEGER NOT NULL DEFAULT 0', 'portrait_error': 'TEXT'},
    'block': {'scene_state': "TEXT NOT NULL DEFAULT 'pending' CHECK (scene_state IN ('pending', 'scanning', 'done'))"},
    'moment': {'wanted': 'INTEGER NOT NULL DEFAULT 0', 'starred': 'INTEGER NOT NULL DEFAULT 0',
               'title': "TEXT NOT NULL DEFAULT ''", 'summary': "TEXT NOT NULL DEFAULT ''",
               'importance': 'INTEGER NOT NULL DEFAULT 5'},
    'segment': {'emotion': EMOTION}}


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
        if ('moment', 'importance') in added:
            # Until now only the few best moments were kept. Pages are looked through again as they are read, for
            # every moment and how much each one matters.
            self.run("UPDATE block SET scene_state = 'pending' WHERE scene_state = 'done'")
        # Work interrupted by a restart goes back in the queue.
        self.run("UPDATE block SET tag_state = 'pending' WHERE tag_state = 'tagging'")
        self.run("UPDATE segment SET audio_state = 'none' WHERE audio_state = 'generating'")
        self.run("UPDATE character SET voice_state = 'none' WHERE voice_state = 'designing'")
        self.run("UPDATE character SET draft_state = 'queued' WHERE draft_state = 'designing'")
        self.run("UPDATE block SET scene_state = 'pending' WHERE scene_state = 'scanning'")
        self.run("UPDATE character SET portrait_state = 'none' WHERE portrait_state = 'drawing'")
        self.run("UPDATE moment SET image_state = 'none' WHERE image_state = 'drawing'")
        self.run("UPDATE voice_take SET state = 'queued' WHERE state = 'making'")

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

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def reconcile_generation(self, identity, legacy_identity):
        """Select the library's narrator before workers start, invalidating only clips when its identity changes."""
        encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'))
        legacy = json.dumps(legacy_identity, sort_keys=True, separators=(',', ':'))
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                saved = self.one('SELECT identity FROM generation_identity WHERE singleton = 1')
                changed = (saved['identity'] if saved else legacy) != encoded
                if changed:
                    self.invalidate_audio('1')
                self.db.execute('INSERT INTO generation_identity (singleton, identity) VALUES (1, ?) '
                                'ON CONFLICT (singleton) DO UPDATE SET identity = excluded.identity', (encoded,))
                self.db.execute('COMMIT')
                return changed
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
        return self.all('SELECT id, title, author, block_count, position_block, position_seq, language FROM book ORDER BY created_at DESC, id DESC')

    def book(self, book_id):
        book = self.one('SELECT id, title, author, block_count, position_block, position_seq, language FROM book WHERE id = ?', (book_id,))
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

    def characters_where(self, where, args=()):
        """Characters with their takes, one for each emotion other than neutral."""
        with self.lock:
            characters = self.all(f"SELECT {CHARACTER} FROM character c WHERE {where} ORDER BY c.role != 'narrator', c.id", args)
            ids = [c['id'] for c in characters]
            made = {(t.pop('character_id'), t['emotion']): t for t in self.all(
                f'SELECT t.character_id, t.emotion, {TAKE_STATE} AS state, t.rev FROM voice_take t '
                f"JOIN character c ON c.id = t.character_id WHERE t.character_id IN ({','.join('?' * len(ids))})", ids)}
        for c in characters:
            c['takes'] = [made.get((c['id'], emotion), {'emotion': emotion, 'state': 'none', 'rev': 0})
                          for emotion in DIRECTIONS]
        return characters

    def characters(self, book_id):
        return self.characters_where('c.book_id = ?', (book_id,))

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

    def take(self, character_id, emotion):
        return next(t for t in self.character(character_id)['takes'] if t['emotion'] == emotion)

    def request_take(self, character_id, emotion):
        """Have a take made now, or made again. Once it is ready, the lines read in it are read again."""
        self.run("INSERT INTO voice_take (character_id, emotion, state) VALUES (?, ?, 'queued') "
                 "ON CONFLICT (character_id, emotion) DO UPDATE SET state = 'queued', rev = rev + 1, error = NULL",
                 (character_id, emotion))

    def merge_character(self, character_id, into):
        with self.lock:
            self.run('UPDATE segment SET character_id = ? WHERE character_id = ?', (into, character_id))
            self.invalidate_audio('character_id = ?', (into,))
            # A moment that shows both keeps the survivor's row; the other goes with the deleted character.
            self.run('UPDATE OR IGNORE moment_character SET character_id = ? WHERE character_id = ?', (into, character_id))
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
                self.run(f"UPDATE segment SET character_id = NULL, emotion = 'neutral' "
                         f"WHERE block_id IN ({marks}) AND kind = 'speech'", ids)
                self.invalidate_audio(f"block_id IN ({marks}) AND kind = 'speech'", ids)
                self.run(f"UPDATE block SET tag_state = 'pending' WHERE id IN ({marks})", ids)
            return ids

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
