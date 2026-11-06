import io

import pytest
import soundfile as sf

from meread import jobs, speech
from meread.store import Store
from test_pipeline import focus

T3_ORIGINS = ['t3code://app', 't3code-dev://app', 'http://localhost:3773', 'http://127.0.0.1:3773']


def voiced(world):
    narrator = world.store.narrator(world.book['id'])
    world.pipeline.design_voice(narrator)
    return world.store.character(narrator['id'])


def ask(world, voice, *texts):
    return world.client.post('/api/clips', json={'voice': voice['id'], 'texts': list(texts)})


def clip(world, key):
    return world.client.get(f'/api/clips/{key}').json()


def said(world):
    return [text for text, _, _ in world.engine.spoken]


def read_all(world):
    while world.pipeline.speech_step():
        pass


def test_new_clips_are_queued_and_read_in_the_order_asked(world):
    texts = ['Echo.', 'Alpha.', '“Delta,” she said.', 'Charlie.', 'Bravo.']
    reply = ask(world, voiced(world), *texts)
    assert reply.status_code == 200
    clips = reply.json()
    assert [set(c) for c in clips] == [{'key', 'state', 'duration', 'error'}] * 5
    assert [(c['state'], c['duration'], c['error']) for c in clips] == [('queued', None, None)] * 5
    assert len({c['key'] for c in clips}) == 5
    read_all(world)
    assert said(world) == ['Echo.', 'Alpha.', 'Delta, she said.', 'Charlie.', 'Bravo.']
    assert [clip(world, c['key'])['state'] for c in clips] == ['ready'] * 5


def test_a_new_request_drops_the_clips_it_leaves_out_and_keeps_ready_ones(world):
    voice = voiced(world)
    one, two, three = ask(world, voice, 'One.', 'Two.', 'Three.').json()
    world.pipeline.speech_step()
    again, four = ask(world, voice, 'One.', 'Four.').json()
    assert again == {**one, 'state': 'ready', 'duration': 0.5}
    assert four['state'] == 'queued'
    assert [clip(world, c['key'])['state'] for c in (two, three)] == ['idle', 'idle']
    read_all(world)
    assert said(world) == ['One.', 'Four.']
    assert ask(world, voice, 'Two.').json() == [{**two, 'state': 'queued'}]


def test_a_clip_being_read_finishes_when_a_new_request_leaves_it_out(world):
    voice = voiced(world)
    first = ask(world, voice, 'One.').json()[0]
    world.engine.during_speak = lambda: ask(world, voice, 'Two.')
    world.pipeline.speech_step()
    world.engine.during_speak = None
    assert clip(world, first['key'])['state'] == 'ready'
    read_all(world)
    assert said(world) == ['One.', 'Two.']


def test_asking_again_with_the_same_body_changes_nothing(world):
    voice = voiced(world)
    texts = ['One.', 'Two.', 'Three.']
    first = ask(world, voice, *texts).json()
    assert ask(world, voice, *texts).json() == first
    read_all(world)
    assert said(world) == texts
    ready = ask(world, voice, *texts).json()
    assert [c['key'] for c in ready] == [c['key'] for c in first]
    assert [c['state'] for c in ready] == ['ready'] * 3
    read_all(world)
    assert said(world) == texts


def test_surrounding_whitespace_reads_the_same_clip(world):
    voice = voiced(world)
    assert ask(world, voice, '  Hello there.\n').json() == ask(world, voice, 'Hello there.').json()


def test_clips_are_read_before_the_book_in_focus(world):
    voice = voiced(world)
    focus(world)
    ask(world, voice, 'Read this first.')
    world.pipeline.speech_step()
    world.pipeline.speech_step()
    assert said(world) == ['Read this first.', 'Chapter One']


def test_audio_is_served_once_the_clip_is_read(world):
    key = ask(world, voiced(world), 'Listen.').json()[0]['key']
    assert world.client.get(f'/api/clips/{key}/audio').status_code == 404
    world.pipeline.speech_step()
    reply = world.client.get(f'/api/clips/{key}/audio')
    assert reply.status_code == 200
    assert reply.headers['content-type'] == 'audio/flac'
    assert reply.headers['cache-control'] == 'private, max-age=31536000, immutable'
    audio, rate = sf.read(io.BytesIO(reply.content))
    assert len(audio) / rate == clip(world, key)['duration'] == 0.5


def test_a_long_text_is_read_in_segment_sized_pieces_and_served_as_one_clip(world):
    sentence = ' '.join(['word'] * 29) + '.'
    key = ask(world, voiced(world), f'{sentence} {sentence}').json()[0]['key']
    world.pipeline.speech_step()
    assert said(world) == [sentence, sentence]
    assert clip(world, key)['duration'] == pytest.approx(0.5 + 0.4 + 0.5)


def test_unknown_voice_is_not_found_and_a_voice_not_ready_is_refused(world):
    assert world.client.post('/api/clips', json={'voice': 99999, 'texts': ['Hi.']}).status_code == 404
    assert world.client.get('/api/clips/0123456789abcdef0123456789abcdef').status_code == 404
    assert world.client.get('/api/clips/0123456789abcdef0123456789abcdef/audio').status_code == 404
    reply = ask(world, world.store.narrator(world.book['id']), 'Hi.')
    assert reply.status_code == 409
    assert 'Narrator' in reply.json()['detail'] and 'not ready' in reply.json()['detail']
    world.pipeline.speech_step()
    assert said(world) == []


@pytest.mark.parametrize('texts', [[], ['x.'] * 21, ['   '], ['x' * 1001]])
def test_requests_outside_the_limits_are_rejected(world, texts):
    assert world.client.post('/api/clips', json={'voice': voiced(world)['id'], 'texts': texts}).status_code == 422


def test_the_limits_count_text_without_surrounding_whitespace(world):
    assert ask(world, voiced(world), *[f"  {n:02}{'x' * 998}  " for n in range(20)]).status_code == 200


def test_a_failed_clip_reports_its_error_until_a_restart(world):
    voice = voiced(world)
    speak = world.engine.speak

    def broken(*args, **kwargs):
        raise RuntimeError('the model crashed')
    world.engine.speak = broken
    key = ask(world, voice, 'Oops.').json()[0]['key']
    world.pipeline.speech_step()
    world.engine.speak = speak
    assert ask(world, voice, 'Oops.').json()[0] == {'key': key, 'state': 'failed', 'duration': None,
                                                    'error': 'the model crashed'}
    world.pipeline.speech_step()
    assert said(world) == []
    Store(world.store.home)
    assert ask(world, voice, 'Oops.').json()[0]['state'] == 'queued'
    world.pipeline.speech_step()
    assert said(world) == ['Oops.']


def test_an_unavailable_speech_server_puts_the_clip_back_in_the_queue(world):
    voice = voiced(world)
    speak = world.engine.speak

    def down(*args, **kwargs):
        raise speech.Unavailable('down')
    world.engine.speak = down
    key = ask(world, voice, 'Later.').json()[0]['key']
    world.pipeline.speech_step()
    assert clip(world, key)['state'] == 'queued'
    assert world.pipeline.idle['speech'] == 'The speech server is unavailable; trying again shortly'
    world.engine.speak = speak
    assert not world.pipeline.speech_step()
    world.pipeline.retry_at.clear()
    world.pipeline.speech_step()
    assert clip(world, key)['state'] == 'ready'


def test_a_restart_keeps_ready_clips_and_forgets_the_queue(world):
    voice = voiced(world)
    ready = ask(world, voice, 'Done.').json()[0]['key']
    world.pipeline.speech_step()
    reading, waiting = (c['key'] for c in ask(world, voice, 'Reading.', 'Waiting.').json())
    jobs.CLIP.claim(world.store, reading)
    Store(world.store.home)
    assert [clip(world, key)['state'] for key in (ready, reading, waiting)] == ['ready', 'idle', 'idle']
    assert not world.pipeline.speech_step()


def test_a_new_voice_or_narrator_reads_new_clips(world):
    voice = voiced(world)
    old = ask(world, voice, 'Hi.').json()[0]
    world.pipeline.speech_step()
    world.store.reset_voice(voice['id'])
    world.pipeline.design_voice(world.store.character(voice['id']))
    new = ask(world, voice, 'Hi.').json()[0]
    assert new['key'] != old['key'] and new['state'] == 'queued'
    assert clip(world, old['key'])['state'] == 'ready'
    world.engine.narrator = speech.NARRATORS['ming']
    assert ask(world, voice, 'Hi.').json()[0]['key'] not in (old['key'], new['key'])


def test_a_clip_read_while_its_voice_is_replaced_lands_nowhere(world):
    voice = voiced(world)
    key = ask(world, voice, 'Hi.').json()[0]['key']
    world.engine.during_speak = lambda: world.store.reset_voice(voice['id'])
    world.pipeline.speech_step()
    assert clip(world, key)['state'] == 'idle'
    assert not world.store.clip_path(key).exists()


def test_deleting_the_book_of_a_voice_removes_its_clips(world):
    key = ask(world, voiced(world), 'Hi.').json()[0]['key']
    assert world.client.delete(f"/api/books/{world.book['id']}").status_code == 204
    assert world.client.get(f'/api/clips/{key}').status_code == 404


def preflight(world, origin):
    return world.client.options('/api/clips', headers={
        'Origin': origin, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})


@pytest.mark.parametrize('origin', T3_ORIGINS)
def test_t3_pages_may_call_the_api(world, origin):
    reply = preflight(world, origin)
    assert reply.status_code == 200
    assert reply.headers['access-control-allow-origin'] == origin
    assert world.client.get('/api/voices', headers={'Origin': origin}).headers['access-control-allow-origin'] == origin


@pytest.mark.parametrize('origin', ['http://evil.example', 'http://localhost:3774', 't3code://evil', 'null'])
def test_other_pages_may_not(world, origin):
    reply = preflight(world, origin)
    assert reply.status_code == 400
    assert 'access-control-allow-origin' not in reply.headers
    assert 'access-control-allow-origin' not in world.client.get('/api/voices', headers={'Origin': origin}).headers
