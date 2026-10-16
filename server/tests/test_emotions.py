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



def test_a_library_from_before_yelling_lets_lines_and_takes_be_yelled(tmp_path):
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
          audio_state TEXT NOT NULL DEFAULT 'none', audio_rev INTEGER NOT NULL DEFAULT 0, duration REAL, error TEXT,
          emotion TEXT NOT NULL DEFAULT 'neutral' CHECK (emotion IN ('neutral', 'happy', 'sad', 'angry', 'scared', 'whisper')));
        CREATE TABLE voice_take (character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
          emotion TEXT NOT NULL CHECK (emotion IN ('happy', 'sad', 'angry', 'scared', 'whisper')),
          state TEXT NOT NULL DEFAULT 'none' CHECK (state IN ('none', 'queued', 'making', 'ready', 'failed')),
          rev INTEGER NOT NULL DEFAULT 0, made_from INTEGER, error TEXT, PRIMARY KEY (character_id, emotion));
        INSERT INTO book (id, title, author, block_count) VALUES (1, 'Old', '', 1);
        INSERT INTO block VALUES (1, 1, 0, 0, 'p', 'tagged');
        INSERT INTO character (id, book_id, name, role, gender, age, pitch, voice_state) VALUES (1, 1, 'Ann', 'named', 'female', 'adult', 'low', 'ready');
        INSERT INTO segment (id, book_id, block_id, block_idx, seq, text, kind, character_id, audio_state, emotion)
          VALUES (1, 1, 1, 0, 0, '“Over here!”', 'speech', 1, 'ready', 'angry');
    """)
    old.close()
    store = Store(tmp_path)
    assert store.segment(1)['emotion'] == 'angry'
    assert store.correct_line(1, emotion='yelling')['emotion'] == 'yelling'
    store.request_take(1, 'yelling')
    assert store.take(1, 'yelling')['state'] == 'queued'
    assert store.one('PRAGMA integrity_check')['integrity_check'] == 'ok'


def cast(w, book_id=None):
    return {c['name']: c for c in w.store.characters(book_id or w.book['id'])}


def heard(w, text):
    """The voice file the last reading of a line was spoken from."""
    return next(voice for said, voice, _ in reversed(w.engine.spoken) if said == text)


def test_a_line_in_an_emotion_is_read_from_a_take_made_once(world):
    world.ask.answer = told([('n1', None), ('n2', 'angry'), ('n2', 'angry'), ('n3', None)])
    focus(world)
    world.drain()
    tom = cast(world)['Tom']['id']
    assert world.engine.directed == [(f'{tom}.wav', 'angry')]
    assert (heard(world, 'Only me'), heard(world, 'Open up.')) == (f'{tom}.angry.wav', f'{tom}.angry.wav')
    assert heard(world, 'Who goes there?') == f"{cast(world)['Guard 1']['id']}.wav"
    assert world.store.take_path(cast(world)['Tom'], 'angry').exists()


def test_a_take_that_cannot_be_made_leaves_the_line_in_the_characters_own_voice(world):
    world.engine.direct_error = RuntimeError('Breeze is not installed')
    world.ask.answer = told([('n1', None), ('n2', 'angry'), ('n2', 'angry'), ('n3', None)])
    focus(world)
    world.drain()
    tom = cast(world)['Tom']['id']
    assert len(world.engine.directed) == 1, 'a failed take is not tried again for every line'
    assert (heard(world, 'Only me'), heard(world, 'Open up.')) == (f'{tom}.wav', f'{tom}.wav')
    assert all(s['audio_state'] == 'ready' for b in world.chapter() for s in b['segments'])
    assert world.store.take(tom, 'angry')['state'] == 'failed'


def test_a_new_voice_makes_its_takes_again_when_a_line_needs_them(world):
    world.ask.answer = told([('n1', None), ('n2', 'angry'), ('n2', None), ('n3', None)])
    focus(world)
    world.drain()
    tom = cast(world)['Tom']['id']
    world.client.post(f'/api/characters/{tom}/voice/draft')
    world.drain()
    world.client.post(f'/api/characters/{tom}/voice/draft/approve')
    assert world.store.take(tom, 'angry')['state'] == 'none'
    world.drain()
    assert world.engine.directed == [(f'{tom}.wav', 'angry')] * 2
    assert world.store.take(tom, 'angry')['state'] == 'ready' and heard(world, 'Only me') == f'{tom}.angry.wav'


def test_making_a_take_again_rereads_only_the_lines_in_that_emotion(world):
    world.ask.answer = told([('n1', 'angry'), ('n2', 'angry'), ('n2', 'sad'), ('n3', None)])
    focus(world)
    world.drain()
    tom = cast(world)['Tom']['id']
    spoken, directed = len(world.engine.spoken), len(world.engine.directed)
    asked = world.client.post(f'/api/characters/{tom}/takes/angry').json()
    assert asked['takes'][2] == {'emotion': 'angry', 'state': 'queued', 'rev': 1}
    world.drain()
    assert world.engine.directed[directed:] == [(f'{tom}.wav', 'angry')]
    assert [text for text, _, _ in world.engine.spoken[spoken:]] == ['Only me']
    assert world.store.take(tom, 'angry') == {'emotion': 'angry', 'state': 'ready', 'rev': 1}


def test_a_spanish_book_reads_every_line_in_the_characters_own_voice(world):
    from conftest import SPANISH, make_epub
    book = world.client.post('/api/books', files={'file': ('es.epub', make_epub((SPANISH,)), 'application/epub+zip')}).json()
    world.ask.answer = lambda prompt: {
        'new_characters': [{'ref': 'n1', 'name': 'Guardia 1', 'role': 'extra', 'gender': 'male', 'age': 'adult',
                            'pitch': 'low', 'description': 'gruff sentry'}],
        'lines': [{'q': i + 1, 'speaker': 'n1', 'emotion': 'angry'} for i in range(prompt.count('[[q'))]}
    world.client.post(f"/api/books/{book['id']}/focus", json={'first': 0, 'last': 5, 'current_first': 0, 'current_last': 5})
    world.drain()
    guard = cast(world, book['id'])['Guardia 1']['id']
    assert world.engine.directed == []
    assert heard(world, '¿Quién anda ahí?') == f'{guard}.wav'
    assert world.store.one('SELECT emotion FROM segment WHERE book_id = ? AND character_id = ? LIMIT 1',
                           (book['id'], guard))['emotion'] == 'angry'


def test_merging_a_character_removes_its_takes(world):
    world.ask.answer = told([('n1', None), ('n2', 'angry'), ('n2', None), ('n3', None)])
    focus(world)
    world.drain()
    tom, anna = cast(world)['Tom'], cast(world)['Anna']
    world.client.post(f"/api/characters/{tom['id']}/merge", json={'into': anna['id']})
    assert not world.store.take_path(tom, 'angry').exists()
    assert world.store.all('SELECT * FROM voice_take WHERE character_id = ?', (tom['id'],)) == []
    world.drain()
    assert heard(world, 'Only me') == f"{anna['id']}.angry.wav"


def test_the_cast_shows_every_take_and_plays_the_ready_ones(world):
    world.ask.answer = told([('n1', None), ('n2', 'angry'), ('n2', None), ('n3', None)])
    focus(world)
    world.drain()
    tom = next(c for c in world.client.get(f"/api/books/{world.book['id']}").json()['characters'] if c['name'] == 'Tom')
    assert [(t['emotion'], t['state']) for t in tom['takes']] == [
        ('happy', 'none'), ('sad', 'none'), ('angry', 'ready'), ('scared', 'none'), ('whisper', 'none'), ('yelling', 'none')]
    url = f"/api/characters/{tom['id']}/takes"
    reply = world.client.get(f'{url}/angry/audio')
    assert reply.status_code == 200 and reply.content == world.store.take_path(tom, 'angry').read_bytes()
    assert world.client.get(f'{url}/sad/audio').status_code == 404
    assert world.client.get(f'{url}/neutral/audio').status_code == 404
    assert world.client.post(f'{url}/bored').status_code == 404

    world.client.post(f'{url}/sad')
    world.drain()
    assert world.client.get(f'{url}/sad/audio').status_code == 200
    assert [text for text, _, _ in world.engine.spoken if text == 'Open up.'] == ['Open up.'], \
        'a take nobody speaks in reads nothing again'


def test_a_take_is_only_made_from_a_voice_of_an_english_book(world):
    from conftest import SPANISH, make_epub
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/takes/happy").status_code == 422, 'no voice yet'
    book = world.client.post('/api/books', files={'file': ('es.epub', make_epub((SPANISH,)), 'application/epub+zip')}).json()
    narrador = world.store.narrator(book['id'])
    world.store.run("UPDATE character SET voice_state = 'ready' WHERE id = ?", (narrador['id'],))
    assert world.client.post(f"/api/characters/{narrador['id']}/takes/happy").status_code == 422
    assert world.store.take(narrador['id'], 'happy')['state'] == 'none'
