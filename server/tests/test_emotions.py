import sqlite3

from meread.store import Store
from test_pipeline import CAST, focus

# Guard 1 shouts, Tom answers calmly and then whispers, Anna is angry.
FEELINGS = [('n1', 'angry'), ('n2', None), ('n2', 'whisper'), ('n3', 'angry')]


def told(feelings):
    def answer(prompt):
        lines = [{'q': q + 1, 'speaker': speaker, **({'emotion': emotion} if emotion else {})}
                 for q, (speaker, emotion) in enumerate(feelings[:prompt.count('[[q')])]
        return {'new_characters': CAST, 'lines': lines}
    return answer


def lines(w, chapter=0):
    names = {c['id']: c['name'] for c in w.store.characters(w.book['id'])}
    return [(s['text'], names.get(s['character_id']), s['emotion'])
            for b in w.chapter(chapter) for s in b['segments'] if s['kind'] == 'speech']


def test_the_tagger_says_how_each_line_is_said(world):
    world.ask.answer = told(FEELINGS)
    focus(world)
    world.drain()
    assert lines(world) == [('“Who goes there?”', 'Guard 1', 'angry'), ('“Only me,”', 'Tom', 'neutral'),
                            ('“Open up.”', 'Tom', 'whisper'), ('“You are late,”', 'Anna', 'angry')]
    assert all(s['emotion'] == 'neutral' for b in world.chapter() for s in b['segments'] if s['kind'] == 'narration')


def test_an_unknown_emotion_and_a_line_for_the_narrator_are_neutral(world):
    world.ask.answer = told([('n1', 'furious'), ('narrator', 'angry'), ('n2', 'sad'), ('n3', None)])
    focus(world)
    world.drain()
    assert [emotion for _, _, emotion in lines(world)] == ['neutral', 'neutral', 'sad', 'neutral']


def test_changing_how_a_line_is_said_reads_it_again(world):
    world.ask.answer = told(FEELINGS)
    focus(world)
    world.drain()
    line = next(s for b in world.chapter() for s in b['segments'] if s['text'] == '“Only me,”')
    changed = world.client.patch(f"/api/segments/{line['id']}", json={'emotion': 'sad'}).json()
    assert (changed['emotion'], changed['character_id'], changed['audio_state'], changed['audio_rev']) == (
        'sad', line['character_id'], 'none', line['audio_rev'] + 1)
    spoken = len(world.engine.spoken)
    world.drain()
    assert [text for text, _, _ in world.engine.spoken[spoken:]] == ['Only me']
    narration = world.chapter()[1]['segments'][0]
    assert world.client.patch(f"/api/segments/{narration['id']}", json={'emotion': 'angry'}).status_code == 422
    assert world.client.patch(f"/api/segments/{line['id']}", json={'emotion': 'bored'}).status_code == 422
    assert world.client.patch(f"/api/segments/{line['id']}", json={}).status_code == 422


def test_tagging_a_paragraph_again_forgets_how_its_lines_were_said(world):
    world.ask.answer = told(FEELINGS)
    focus(world)
    world.drain()
    block = world.chapter()[2]
    world.ask.answer = lambda prompt: {'new_characters': [], 'lines': [{'q': 1, 'speaker': 'narrator'}]}
    world.client.post(f"/api/blocks/{block['id']}/retag")
    assert world.chapter()[2]['segments'][0]['emotion'] == 'neutral'
    world.drain()
    assert lines(world)[0] == ('“Who goes there?”', 'Narrator', 'neutral')


def test_a_library_from_before_emotions_opens_with_every_line_neutral(tmp_path):
    old = sqlite3.connect(tmp_path / 'library.db')
    old.executescript("""
        CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, block_count INTEGER NOT NULL,
          position_block INTEGER NOT NULL DEFAULT 0, position_seq INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, language TEXT NOT NULL DEFAULT 'en');
        CREATE TABLE block (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
          idx INTEGER NOT NULL, chapter INTEGER NOT NULL, tag TEXT NOT NULL, tag_state TEXT NOT NULL, UNIQUE (book_id, idx));
        CREATE TABLE character (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
          name TEXT NOT NULL, role TEXT NOT NULL, gender TEXT NOT NULL, age TEXT NOT NULL, pitch TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', voice_state TEXT NOT NULL DEFAULT 'none', voice_rev INTEGER NOT NULL DEFAULT 0,
          error TEXT);
        CREATE TABLE segment (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
          block_id INTEGER NOT NULL REFERENCES block(id) ON DELETE CASCADE, block_idx INTEGER NOT NULL, seq INTEGER NOT NULL,
          text TEXT NOT NULL, kind TEXT NOT NULL, character_id INTEGER REFERENCES character(id),
          audio_state TEXT NOT NULL DEFAULT 'none', audio_rev INTEGER NOT NULL DEFAULT 0, duration REAL, error TEXT);
        INSERT INTO book (id, title, author, block_count) VALUES (1, 'Old', '', 1);
        INSERT INTO block VALUES (1, 1, 0, 0, 'p', 'tagged');
        INSERT INTO character (id, book_id, name, role, gender, age, pitch, voice_state) VALUES (1, 1, 'Ann', 'named', 'female', 'adult', 'low', 'ready');
        INSERT INTO segment (id, book_id, block_id, block_idx, seq, text, kind, character_id, audio_state)
          VALUES (1, 1, 1, 0, 0, '“Go!”', 'speech', 1, 'ready');
    """)
    old.close()
    store = Store(tmp_path)
    assert (store.segment(1)['emotion'], store.segment(1)['audio_state']) == ('neutral', 'ready')
