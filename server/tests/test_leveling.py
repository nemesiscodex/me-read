import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from meread import speech
from meread.app import create_app
from test_pipeline import focus

RATE = 24000


def speaking(level, hz=150, seconds=3):
    """A voiced tone in syllables with pauses, its loud parts at about `level` in amplitude."""
    t = np.arange(int(seconds * RATE)) / RATE
    tone = sum(np.sin(2 * np.pi * hz * k * t) / k for k in (1, 2, 3)) / 1.6
    syllables = np.clip(np.sin(2 * np.pi * 3 * t), 0, None) * (t % 1 < 0.7)
    return (level * tone * syllables).astype('float32')


def level_of(path):
    audio, rate = sf.read(path, dtype='float32')
    return speech.loudness(audio, rate)


def test_quiet_and_loud_voices_end_at_the_same_level():
    quiet, loud = speech.leveled(speaking(0.06, 220), RATE), speech.leveled(speaking(0.5, 110), RATE)
    assert speech.loudness(quiet, RATE) == pytest.approx(speech.loudness(loud, RATE), abs=0.5)
    assert speech.loudness(quiet, RATE) == pytest.approx(speech.TARGET, abs=0.5)


def test_a_click_keeps_a_quiet_line_from_being_raised_past_the_peak_ceiling():
    audio = speaking(0.03)
    audio[RATE] = 0.8
    out = speech.leveled(audio, RATE)
    assert np.abs(out).max() == pytest.approx(10 ** (speech.PEAK / 20), rel=1e-3)
    assert speech.loudness(out, RATE) < speech.TARGET - 1


def test_a_nearly_silent_line_is_raised_by_no_more_than_the_boost_ceiling():
    audio = speaking(0.006)
    np.testing.assert_allclose(speech.leveled(audio, RATE), audio * 10 ** (speech.BOOST / 20), rtol=1e-5)


@pytest.mark.parametrize('audio', [np.zeros(RATE, dtype='float32'), np.full(RATE, 1e-5, dtype='float32'),
                                   np.full(RATE, 0.1, dtype='float32')])
def test_silence_and_a_constant_offset_are_left_as_they_are(audio):
    assert speech.leveled(audio, RATE) is audio


def test_leveling_twice_is_leveling_once():
    once = speech.leveled(speaking(0.05), RATE)
    np.testing.assert_allclose(speech.leveled(once, RATE), once, atol=1e-4)


def clip_row(store, segment_id):
    return store.one('SELECT audio_state, audio_rev, duration, leveled FROM segment WHERE id = ?', (segment_id,))


def test_a_new_clip_lands_at_the_shared_level(world, tmp_path):
    focus(world, 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    engine = speech.Engine(tmp_path / 'hub')
    engine.raw = engine.raw._replace(line=lambda text, voice, seed, language: (speaking(0.08), RATE))
    world.pipeline.engine = engine
    assert world.pipeline.speech_step()
    line = world.chapter()[0]['segments'][0]
    path = world.store.audio_path(world.book['id'], line['id'])
    assert level_of(path) == pytest.approx(speech.TARGET, abs=0.5)
    assert clip_row(world.store, line['id'])['leveled'] == 1
    landed = path.read_bytes()
    world.drain()
    assert path.read_bytes() == landed, 'a clip leveled when it landed is not leveled again'


def test_a_library_from_before_leveling_has_its_clips_leveled_once_and_in_place(world, tmp_path):
    focus(world, 0, 1, 0, 1)
    world.drain()
    lines = [s for b in world.chapter()[:2] for s in b['segments']]
    paths = [world.store.audio_path(world.book['id'], s['id']) for s in lines]
    for path, level in zip(paths, (0.08, 0.5)):
        sf.write(path, speaking(level), RATE, format='FLAC', subtype='PCM_16')
    before = [clip_row(world.store, s['id']) for s in lines]
    world.store.run('DROP INDEX segment_unleveled')
    world.store.run('ALTER TABLE segment DROP COLUMN leveled')

    reopened = create_app(tmp_path, engine=world.engine, run_workers=False)
    with TestClient(reopened):
        store, pipeline = reopened.state.store, reopened.state.pipeline
        assert {row['leveled'] for row in store.all('SELECT leveled FROM segment')} == {0}
        assert pipeline.speech_step()
        assert not pipeline.speech_step()
        assert [level_of(path) for path in paths] == [pytest.approx(speech.TARGET, abs=0.5)] * 2
        assert [clip_row(store, s['id']) for s in lines] == [{**row, 'leveled': 1} for row in before]
        assert sf.info(paths[0]).subtype == 'PCM_16'


@pytest.mark.parametrize('meanwhile', ['read again', 'dropped'])
def test_a_clip_changed_while_it_is_leveled_keeps_its_change(world, monkeypatch, meanwhile):
    focus(world, 0, 0, 0, 0)
    world.drain()
    line = world.chapter()[0]['segments'][0]
    path = world.store.audio_path(world.book['id'], line['id'])
    sf.write(path, speaking(0.08), RATE, format='FLAC', subtype='PCM_16')
    world.store.run('UPDATE segment SET leveled = 0 WHERE id = ?', (line['id'],))
    leveled = speech.leveled

    def change_then_level(audio, rate):
        world.client.post(f"/api/segments/{line['id']}/regenerate")
        if meanwhile == 'read again':
            world.drain()
        return leveled(audio, rate)
    monkeypatch.setattr(speech, 'leveled', change_then_level)
    on_disk = path.read_bytes()

    assert world.pipeline.level_step()
    monkeypatch.setattr(speech, 'leveled', leveled)
    row = clip_row(world.store, line['id'])
    if meanwhile == 'read again':
        assert (row['audio_state'], row['audio_rev'], row['leveled']) == ('ready', line['audio_rev'] + 1, 1)
        assert np.allclose(sf.read(path, dtype='float32')[0], 0.1, atol=1e-3), 'the new reading stays'
    else:
        assert (row['audio_state'], row['leveled']) == ('none', 0)
        assert path.read_bytes() == on_disk
    assert not world.pipeline.level_step()
