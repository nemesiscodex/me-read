import numpy as np
import pytest

from meread import speech
from test_emotions import cast, told
from test_pipeline import focus


def wav(level):
    return speech.wav_of(np.full(24000 * 7, level, dtype='float32'), 24000)


def upload(world, tom, anna):
    sample = wav(0.3)
    world.store.replace_voice(tom['id'], sample)
    return sample


def approve(world, tom, anna):
    world.store.draft_path(tom).write_bytes(wav(0.4))
    assert world.store.approve_draft(tom['id'])
    return wav(0.4)


def reuse(world, tom, anna):
    world.store.voice_path(anna).write_bytes(wav(0.5))
    assert world.store.reuse_voice(tom['id'], anna['id'], anna['voice_rev'])
    return wav(0.5)


@pytest.mark.parametrize('replace, draft_after', [
    (upload, lambda before: (before['draft_state'], before['draft_rev'], True)),
    (approve, lambda before: ('none', before['draft_rev'], False)),
    (reuse, lambda before: ('none', before['draft_rev'] + 1, False)),
])
def test_a_new_voice_drops_everything_made_in_the_old_one(world, replace, draft_after):
    world.ask.answer = told([('n1', None), ('n2', 'angry'), ('n2', None), ('n3', None)])
    focus(world)
    world.drain()
    tom, anna = cast(world)['Tom'], cast(world)['Anna']
    ready = world.client.post('/api/clips', json={'voice': tom['id'], 'texts': ['Hi.']}).json()[0]['key']
    world.drain()
    world.client.post(f"/api/characters/{tom['id']}/voice/draft")
    world.drain()
    queued = world.client.post('/api/clips', json={'voice': tom['id'], 'texts': ['Bye.']}).json()[0]['key']
    before, take = world.store.character(tom['id']), world.store.take(tom['id'], 'angry')
    lines = {s['id']: s for b in world.chapter() for s in b['segments']}
    voice = world.store.voice_path(tom)
    assert before['draft_state'] == take['state'] == 'ready'

    with open(voice, 'rb') as reading:
        old = voice.read_bytes()
        sample = replace(world, tom, anna)
        assert reading.read() == old, 'a reader of the old voice gets all of it'
    assert voice.read_bytes() == sample

    after = world.store.character(tom['id'])
    assert (after['voice_state'], after['voice_rev'], after['error']) == ('ready', before['voice_rev'] + 1, None)
    assert (after['draft_state'], after['draft_rev'], world.store.draft_path(tom).exists()) == draft_after(before)
    assert world.store.take(tom['id'], 'angry') == {**take, 'state': 'none', 'rev': take['rev'] + 1}
    for line in (s for b in world.chapter() for s in b['segments']):
        old_line = lines[line['id']]
        if line['character_id'] == tom['id']:
            assert (line['audio_state'], line['audio_rev']) == ('none', old_line['audio_rev'] + 1)
        else:
            assert line == old_line
    assert [world.store.clip(key)['state'] for key in (ready, queued)] == ['ready', 'idle']
