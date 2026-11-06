import io
import sqlite3

import numpy as np
import pytest
import soundfile as sf

from meread import speech
from meread.store import Store
from test_emotions import cast, told
from test_pipeline import answer, focus

RATE = 24000


def shift(world, character, semitones):
    return world.client.post(f"/api/characters/{character['id']}/voice/shift", json={'semitones': semitones})


def voiced(world, character, hz=150):
    t = np.arange(7 * RATE) / RATE
    tone = sum(0.3 / k * np.sin(2 * np.pi * hz * k * t) for k in (1, 2, 3)).astype('float32')
    sf.write(world.store.voice_path(character), tone, RATE, format='WAV')
    return world.store.voice_path(character).read_bytes()


@pytest.mark.parametrize('semitones', [5, -5])
def test_a_shifted_voice_is_a_candidate_until_it_is_used(world, semitones):
    world.ask.answer = answer
    focus(world)
    world.drain()
    tom = next(c for c in world.store.characters(world.book['id']) if c['name'] == 'Tom')
    world.store.request_take(tom['id'], 'happy')
    world.pipeline.make_take(tom, world.store.take(tom['id'], 'happy'))
    tom = world.store.character(tom['id'])
    assert world.store.take(tom['id'], 'happy')['state'] == 'ready'
    sample = voiced(world, tom)
    before = world.chapter()

    reply = shift(world, tom, semitones)
    assert reply.status_code == 200
    candidate = reply.json()
    assert (candidate['draft_state'], candidate['draft_rev']) == ('ready', tom['draft_rev'] + 1)
    assert (candidate['voice_rev'], candidate['takes']) == (tom['voice_rev'], tom['takes'])
    heard = world.client.get(f"/api/characters/{tom['id']}/voice/draft")
    audio, rate = sf.read(io.BytesIO(heard.content), dtype='float32')
    assert speech.median_pitch(audio, rate) == pytest.approx(150 * 2 ** (semitones / 12), rel=0.02)
    assert world.store.voice_path(tom).read_bytes() == sample
    assert world.chapter() == before

    spoken = len(world.engine.spoken)
    assert world.client.post(f"/api/characters/{tom['id']}/voice/draft/approve").status_code == 200
    used = world.store.character(tom['id'])
    assert (used['voice_state'], used['voice_rev'], used['draft_state']) == ('ready', tom['voice_rev'] + 1, 'none')
    assert world.store.voice_path(tom).read_bytes() == heard.content
    assert world.store.take(tom['id'], 'happy')['state'] == 'none'
    assert {s['audio_state'] for b in world.chapter() for s in b['segments'] if s['character_id'] == tom['id']} == {'none'}
    world.drain()
    assert [text for text, _, _ in world.engine.spoken[spoken:]] == ['Only me', 'Open up.']


@pytest.mark.parametrize('body', [{'semitones': 0}, {'semitones': 13}, {'semitones': -13}, {'semitones': 2.5},
                                  {'semitones': '2'}, {'semitones': True}, {}])
def test_a_shift_must_be_a_whole_number_of_semitones_up_to_an_octave(world, body):
    narrator = world.store.narrator(world.book['id'])
    world.pipeline.design_voice(narrator)
    reply = world.client.post(f"/api/characters/{narrator['id']}/voice/shift", json=body)
    assert reply.status_code == 422
    assert world.store.character(narrator['id'])['draft_state'] == 'none'


def test_a_voice_that_is_not_ready_cannot_be_shifted(world):
    narrator = world.store.narrator(world.book['id'])
    assert shift(world, narrator, 2).status_code == 422
    assert not world.store.draft_path(narrator).exists()
    assert shift(world, {'id': 99999}, 2).status_code == 404


def test_shifting_cancels_a_candidate_that_is_being_designed(world):
    narrator = world.store.narrator(world.book['id'])
    world.pipeline.design_voice(narrator)
    world.store.request_draft(narrator['id'])
    candidate = world.store.character(narrator['id'])
    world.engine.during_design = lambda: shift(world, narrator, 5).raise_for_status()
    world.pipeline.design_draft(candidate)
    changed = world.store.character(narrator['id'])
    assert (changed['draft_state'], changed['draft_status']) == ('ready', None)
    assert changed['draft_rev'] == candidate['draft_rev'] + 1
    assert sf.info(world.store.draft_path(changed)).frames == round(7 * RATE / 2 ** (5 / 12)), 'the shift, not the design'


def shift_take(world, character_id, emotion, body):
    return world.client.put(f'/api/characters/{character_id}/takes/{emotion}/semitones', json=body)


def clips(world):
    return {s['text']: (s['audio_state'], s['audio_rev']) for b in world.chapter() for s in b['segments']}


def test_a_take_cloned_lower_rereads_only_that_characters_lines_in_it_and_survives_a_new_take(world):
    # Guard 1 and Anna are angry too, Tom is angry and then sad.
    world.ask.answer = told([('n1', 'angry'), ('n2', 'angry'), ('n2', 'sad'), ('n3', 'angry')])
    focus(world)
    world.drain()
    tom = cast(world)['Tom']['id']
    before, spoken, directed = clips(world), len(world.engine.spoken), len(world.engine.directed)
    assert world.engine.semitones['Only me'] == 0

    reply = shift_take(world, tom, 'angry', {'semitones': -3})
    assert reply.status_code == 200
    assert next(t for t in reply.json()['takes'] if t['emotion'] == 'angry')['semitones'] == -3
    assert next(t for t in reply.json()['takes'] if t['emotion'] == 'sad')['semitones'] == 0
    after = clips(world)
    assert after.pop('“Only me,”') == ('none', before.pop('“Only me,”')[1] + 1)
    assert after == before
    world.drain()
    assert [text for text, _, _ in world.engine.spoken[spoken:]] == ['Only me']
    assert world.engine.semitones['Only me'] == -3
    assert world.engine.directed[directed:] == [], 'the take itself is kept'

    spoken = len(world.engine.spoken)
    assert shift_take(world, tom, 'angry', {'semitones': -3}).status_code == 200
    world.drain()
    assert world.engine.spoken[spoken:] == [], 'the same setting reads nothing again'

    world.client.post(f'/api/characters/{tom}/takes/angry')
    world.drain()
    assert world.store.take(tom, 'angry')['semitones'] == -3
    assert world.engine.semitones['Only me'] == -3
    world.client.post(f'/api/characters/{tom}/voice/draft')
    world.drain()
    world.client.post(f'/api/characters/{tom}/voice/draft/approve')
    world.drain()
    assert world.store.take(tom, 'angry') == {'emotion': 'angry', 'state': 'ready', 'rev': 2, 'semitones': -3}
    assert (world.engine.semitones['Only me'], world.engine.semitones['Open up.']) == (-3, 0)

    spoken = len(world.engine.spoken)
    assert shift_take(world, tom, 'angry', {'semitones': 0}).status_code == 200
    world.drain()
    assert [text for text, _, _ in world.engine.spoken[spoken:]] == ['Only me']
    assert world.engine.semitones['Only me'] == 0


def test_a_take_shift_is_a_whole_number_of_semitones_for_a_take_the_book_can_use(world):
    from conftest import SPANISH, make_epub
    narrator = world.store.narrator(world.book['id'])['id']
    for body in {'semitones': 13}, {'semitones': -13}, {'semitones': 2.5}, {'semitones': '2'}, {'semitones': True}, {}:
        assert shift_take(world, narrator, 'angry', body).status_code == 422, body
    for emotion in 'neutral', 'bored':
        assert shift_take(world, narrator, emotion, {'semitones': -3}).status_code == 404
    assert shift_take(world, 99999, 'angry', {'semitones': -3}).status_code == 404
    assert world.store.all('SELECT * FROM voice_take') == []

    world.engine.narrator = speech.NARRATORS['fish']
    book = world.client.post('/api/books', files={'file': ('es.epub', make_epub((SPANISH,)), 'application/epub+zip')}).json()
    narrador = world.store.narrator(book['id'])['id']
    assert shift_take(world, narrador, 'angry', {'semitones': -3}).status_code == 422
    assert world.store.take(narrador, 'angry')['semitones'] == 0


def test_a_library_from_before_take_shifts_keeps_its_takes_unshifted(tmp_path):
    made = Store(tmp_path)
    made.run("INSERT INTO book (id, title, author, block_count) VALUES (1, 'Old', '', 0)")
    made.run("INSERT INTO character (id, book_id, name, role, gender, age, pitch, voice_state) "
             "VALUES (1, 1, 'Ann', 'named', 'female', 'child', 'high', 'ready')")
    made.run("INSERT INTO voice_take (character_id, emotion, state, rev, made_from) VALUES (1, 'angry', 'ready', 3, 0)")
    made.db.close()
    old = sqlite3.connect(tmp_path / 'library.db')
    old.execute('ALTER TABLE voice_take DROP COLUMN semitones')
    old.close()
    store = Store(tmp_path)
    assert store.take(1, 'angry') == {'emotion': 'angry', 'state': 'ready', 'rev': 3, 'semitones': 0}
    assert store.shift_take(1, 'yelling', -3) and store.take(1, 'yelling')['semitones'] == -3
    with pytest.raises(sqlite3.IntegrityError):
        store.run("UPDATE voice_take SET semitones = 13 WHERE emotion = 'angry'")
    assert store.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
