from pathlib import Path
from types import SimpleNamespace

import pytest

from meread import jobs, speech
from meread.store import Store
from test_pipeline import focus


def pick(row, *columns):
    return tuple(row[column] for column in columns)


def voiced(world):
    narrator = world.store.narrator(world.book['id'])
    world.pipeline.design_voice(narrator)
    return world.store.character(narrator['id'])


# Each makes a job of its kind wait. `rev` and `read` look up its revision and its (state, error), `change` is what the
# reader does that makes work in flight for it out of date, and `target` is the file it lands in.

def voice(world):
    character = world.store.narrator(world.book['id'])
    now = lambda: world.store.character(character['id'])
    return SimpleNamespace(key=character['id'], rev=lambda: now()['voice_rev'], read=lambda: pick(now(), 'voice_state', 'error'),
                           change=lambda: world.store.reset_voice(character['id']), target=world.store.voice_path(character))


def draft(world):
    character = voiced(world)
    world.store.request_draft(character['id'])
    now = lambda: world.store.character(character['id'])
    return SimpleNamespace(key=character['id'], rev=lambda: now()['draft_rev'], read=lambda: pick(now(), 'draft_state', 'draft_status'),
                           change=lambda: world.store.request_draft(character['id']), target=world.store.draft_path(character))


def take(world):
    character = voiced(world)
    world.store.request_take(character['id'], 'happy')
    now = lambda: world.store.one('SELECT state, rev, error FROM voice_take WHERE character_id = ? AND emotion = ?',
                                  (character['id'], 'happy'))
    return SimpleNamespace(key=(character['id'], 'happy'), rev=lambda: now()['rev'], read=lambda: pick(now(), 'state', 'error'),
                           change=lambda: world.store.request_take(character['id'], 'happy'),
                           target=world.store.take_path(character, 'happy'))


def line(world):
    [segment] = world.chapter()[1]['segments']
    now = lambda: world.store.segment(segment['id'])
    return SimpleNamespace(key=segment['id'], rev=lambda: now()['audio_rev'], read=lambda: pick(now(), 'audio_state', 'error'),
                           change=lambda: world.store.correct_line(segment['id']),
                           target=world.store.audio_path(world.book['id'], segment['id']))


def clip(world):
    character = voiced(world)
    [asked] = world.client.post('/api/clips', json={'voice': character['id'], 'texts': ['Hello.']}).json()
    # A clip has no revision: a new voice gives its text a new clip, and the old one is never asked for again.
    return SimpleNamespace(key=asked['key'], rev=lambda: None, read=lambda: pick(world.store.clip(asked['key']), 'state', 'error'),
                           change=lambda: world.store.reset_voice(character['id']), target=world.store.clip_path(asked['key']))


def portrait(world):
    tom = world.store.add_character(world.book['id'], 'Tom', appearance='tall, with a red scarf')
    now = lambda: world.store.character(tom['id'])
    return SimpleNamespace(key=tom['id'], rev=lambda: now()['portrait_rev'], read=lambda: pick(now(), 'portrait_state', 'portrait_error'),
                           change=lambda: world.store.reset_portrait(tom['id']), target=world.store.portrait_path(tom))


def moment(world):
    block = world.chapter()[2]
    moment_id = world.store.run("INSERT INTO moment (book_id, block_id, block_idx, scene) VALUES (?, ?, ?, 'The gate.')",
                                (world.book['id'], block['id'], block['idx'])).lastrowid
    now = lambda: world.store.moment(moment_id)
    return SimpleNamespace(key=moment_id, rev=lambda: now()['image_rev'], read=lambda: pick(now(), 'image_state', 'error'),
                           change=lambda: world.store.redraw_moments('m.id = ?', (moment_id,)),
                           target=world.store.moment_path(world.book['id'], moment_id))


# The job, how to make one wait, its state while it is worked on, after the service did not answer and after a
# restart, and how much of a failure's reason it keeps.
KINDS = {
    'voice': (jobs.VOICE, voice, 'designing', 'none', 'none', 300),
    'candidate voice': (jobs.DRAFT, draft, 'designing', 'queued', 'queued', 600),
    'take': (jobs.TAKE, take, 'making', 'queued', 'queued', 300),
    'line': (jobs.AUDIO, line, 'generating', 'none', 'none', 300),
    'clip': (jobs.CLIP, clip, 'generating', 'queued', 'idle', 300),
    'portrait': (jobs.PORTRAIT, portrait, 'drawing', 'none', 'none', 300),
    'moment': (jobs.MOMENT, moment, 'drawing', 'none', 'none', 300),
}
each_kind = pytest.mark.parametrize('kind', KINDS)


def claimed(world, kind):
    job, make, working = KINDS[kind][:3]
    made = make(world)
    claim = job.claim(world.store, made.key, made.rev())
    assert made.read()[0] == working
    assert job.claim(world.store, made.key, made.rev()) is None, 'a job is worked on once'
    return claim, made


def write(tmp):
    Path(tmp).write_bytes(b'result')


@each_kind
def test_a_claimed_job_lands_ready_with_its_file(world, kind):
    claim, made = claimed(world, kind)
    assert claim.land(write, made.target)
    assert made.read()[0] == 'ready' and made.target.read_bytes() == b'result'


@each_kind
def test_a_result_finished_after_its_job_changed_lands_nowhere(world, kind):
    job = KINDS[kind][0]
    stale, made = claimed(world, kind)
    made.change()
    newer = job.claim(world.store, made.key, made.rev())
    changed = made.read()
    assert not stale.land(write, made.target)
    assert made.read() == changed and not made.target.exists()
    assert not [p for p in made.target.parent.iterdir() if p.name.startswith('tmp')], 'nothing is left behind'
    if newer:
        assert newer.land(write, made.target), 'the work on the changed job lands'


@each_kind
def test_a_service_that_did_not_answer_puts_the_job_back(world, kind):
    claim, made = claimed(world, kind)
    claim.retry(speech.Unavailable('the speech server is still loading'))
    assert made.read()[0] == KINDS[kind][3]


@each_kind
def test_a_restart_puts_work_in_flight_back(world, kind):
    _, made = claimed(world, kind)
    Store(world.store.home)
    assert made.read()[0] == KINDS[kind][4]


@each_kind
def test_a_failure_keeps_the_start_of_its_reason(world, kind):
    claim, made = claimed(world, kind)
    claim.fail(RuntimeError('x' * 1000))
    assert made.read() == ('failed', 'x' * KINDS[kind][5])


def test_lines_in_a_language_the_narrator_cannot_read_fail_with_the_reason(world):
    world.engine.narrator = speech.NARRATORS['breeze']
    world.store.run("UPDATE book SET language = 'fr' WHERE id = ?", (world.book['id'],))
    focus(world, 0, 1, 0, 1)
    assert world.pipeline.speech_step()
    lines = [segment for block in world.chapter()[:2] for segment in block['segments']]
    assert {(segment['audio_state'], segment['error']) for segment in lines} == {('failed', world.pipeline.idle['speech'])}
    assert 'this book is in fr' in world.pipeline.idle['speech'] and not world.engine.spoken


def test_a_line_whose_speaker_has_no_voice_fails_with_the_whole_reason(world):
    world.engine.design_error = RuntimeError('y' * 1000)
    focus(world, 0, 1, 0, 1)
    world.drain()
    [segment] = world.chapter()[1]['segments']
    assert (segment['audio_state'], segment['error']) == ('failed', 'Narrator has no voice: ' + 'y' * 300)
