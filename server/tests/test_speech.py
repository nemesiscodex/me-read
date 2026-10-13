import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import mlx.core as mx
import numpy as np
import pytest
import soundfile as sf

from meread import speech
from meread.emotions import DIRECTIONS


def recording(path, level=0.1, seconds=7, rate=24000, stereo=False):
    audio = np.full(int(seconds * rate), level, dtype='float32')
    if stereo:
        audio = np.column_stack((audio, audio * 0.5))
    sf.write(path, audio, rate, format='WAV', subtype='PCM_16')
    return path


@pytest.fixture
def models(monkeypatch):
    import mlx_audio.stt
    import mlx_audio.tts.utils

    state = SimpleNamespace(loads=[], snapshots=[], transcribed=[], spoken=[], prepared=[], conditioned=[], error=None,
                            transcript=None, during_transcribe=None, result_rate=None)

    def snapshot(self, repo, revision=None):
        state.snapshots.append((repo, revision))
        return repo

    def result(rate, seconds=0.5):
        rate = state.result_rate or rate
        audio = mx.full((1, int(rate * seconds)), 0.1 + float(mx.random.uniform()) * 0.1)
        return SimpleNamespace(audio=audio, sample_rate=rate)

    class Whisper:
        def generate(self, audio):
            state.transcribed.append(np.asarray(audio))
            if state.during_transcribe:
                state.during_transcribe()
            if state.error:
                raise state.error
            text = state.transcript
            if text is None:
                text = 'Primera frase.' if float(audio.mean()) < 0.15 else 'Otra frase.'
            return SimpleNamespace(text=text)

    class Fish:
        sample_rate = 44100

        def generate(self, *, text, ref_audio, ref_text, stream):
            assert isinstance(ref_audio, mx.array)
            state.spoken.append({'text': text, 'audio': np.asarray(ref_audio), 'ref_text': ref_text, 'stream': stream})
            yield result(self.sample_rate)

    class Breeze:
        sample_rate = 24000

        def generate(self, *, text, seed, stream, ref_audio=None, ref_text=None, **direction):
            assert ref_audio is None or isinstance(ref_audio, str)
            reference = {'wav': Path(ref_audio).read_bytes(), 'ref_text': ref_text} if ref_audio else {}
            state.spoken.append({'text': text, **reference, 'seed': seed, 'stream': stream, **direction})
            mx.random.seed(seed)
            yield result(self.sample_rate, seconds=20 if direction else 0.5)

    class Chatterbox:
        sample_rate = 24000
        ve = SimpleNamespace(embeds_from_wavs=lambda *args, **kwargs: np.array([[1., 0., 0.]]))

        def prepare_conditionals(self, path, *args, **kwargs):
            data = Path(path).read_bytes()
            state.prepared.append((data, args, kwargs))
            self._conds = hashlib.sha256(data).hexdigest()
            return self._conds

        def generate(self, **kwargs):
            state.conditioned.append(kwargs.get('conds', self._conds))
            state.spoken.append(kwargs)
            yield result(self.sample_rate)

    class Designer:
        sample_rate = 24000

        def generate_voice_design(self, *, text, instruct, language):
            state.spoken.append({'design_text': text, 'instruction': instruct, 'language': language})
            yield result(self.sample_rate, seconds=20)

    def load(repo):
        state.loads.append(repo)
        return {speech.DESIGNER: Designer, speech.CHATTERBOX: Chatterbox, speech.MULTILINGUAL: Chatterbox,
                speech.PROVIDERS['fish'].models[0]: Fish, speech.PROVIDERS['breeze'].models[0]: Breeze,
                speech.TRANSCRIBER: Whisper}[repo]()

    monkeypatch.setattr(speech.Engine, '_snapshot', snapshot)
    monkeypatch.setattr(mlx_audio.stt, 'load', load)
    monkeypatch.setattr(mlx_audio.tts.utils, 'load_model', load)
    return state


def test_default_english_and_spanish_still_use_chatterbox_without_whisper(tmp_path, models):
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub')
    english, english_rate = engine.speak('Hello.', voice, 42)
    spanish, spanish_rate = engine.speak('Hola.', voice, 42, language='es')
    assert models.loads == [speech.CHATTERBOX, speech.MULTILINGUAL]
    assert models.transcribed == []
    assert (english_rate, spanish_rate) == (24000, 24000)
    assert english.shape == spanish.shape == (12000,)
    assert english.dtype == spanish.dtype == np.float32
    assert models.spoken[0] == {'text': 'Hello.', 'temperature': 0.8, 'top_p': 0.95, 'top_k': 1000,
                                'repetition_penalty': 1.2}
    assert models.spoken[1]['lang_code'] == 'es'
    assert models.prepared[0][2] == {'exaggeration': 0, 'norm_loudness': True}
    assert models.prepared[1][1] == (24000,)


@pytest.mark.parametrize('language', ['en', 'es'])
def test_chatterbox_replacement_during_preparation_cannot_poison_an_identical_reference(tmp_path, models, monkeypatch, language):
    voice = recording(tmp_path / 'voice.wav')
    original = voice.read_bytes()
    untouched = tmp_path / 'untouched.wav'
    untouched.write_bytes(original)
    engine = speech.Engine(tmp_path / 'hub')
    cached = engine._cached

    def replace(cache, key, prepare):
        if cache is engine.voices:
            recording(voice, level=0.2)
            monkeypatch.setattr(engine, '_cached', cached)
        return cached(cache, key, prepare)

    monkeypatch.setattr(engine, '_cached', replace)
    engine.speak('First.', voice, 42, language=language)
    engine.speak('Second.', untouched, 42, language=language)
    replacement = voice.read_bytes()
    assert replacement != original and untouched.read_bytes() == original
    assert models.conditioned[1] == hashlib.sha256(original).hexdigest()
    assert models.conditioned[0] == hashlib.sha256(original).hexdigest()
    assert [data for data, _, _ in models.prepared] == [original]
    engine.speak('Third.', voice, 42, language=language)
    assert models.conditioned[2] == hashlib.sha256(replacement).hexdigest()
    assert [data for data, _, _ in models.prepared] == [original, replacement]


@pytest.mark.parametrize('language', ['en', 'es'])
def test_speaker_replacement_during_preparation_cannot_poison_an_identical_reference(tmp_path, models, monkeypatch, language):
    voice = recording(tmp_path / 'voice.wav', stereo=True, rate=32000)
    original = voice.read_bytes()
    untouched = tmp_path / 'untouched.wav'
    untouched.write_bytes(original)
    engine = speech.Engine(tmp_path / 'hub')
    model = engine._cloner(language)
    references = []

    def hear(wavs, sample_rate):
        audio = np.asarray(wavs[0])
        assert sample_rate == 16000
        if len(audio) == 7 * sample_rate:
            references.append(audio)
            return np.array([[1., 0.]] if float(audio.mean()) < 0.15 else [[0., 1.]])
        return np.array([[0., 1.]] if len(audio) == 15 * sample_rate else [[1., 0.]])

    monkeypatch.setattr(model.ve, 'embeds_from_wavs', hear)
    cached = engine._cached

    def replace(cache, key, prepare):
        if cache is engine.speakers:
            recording(voice, level=0.2, rate=44100)
            monkeypatch.setattr(engine, '_cached', cached)
        return cached(cache, key, prepare)

    monkeypatch.setattr(engine, '_cached', replace)
    first, rate = engine.design('A low voice.', 42, language=language, others=[voice])
    second, _ = engine.design('A low voice.', 42, language=language, others=[untouched])
    assert voice.read_bytes() != original and untouched.read_bytes() == original
    assert len(second) == 15 * rate
    np.testing.assert_array_equal(first, second)
    assert len(references) == 1
    assert float(references[0].mean()) == pytest.approx(0.075, abs=0.0001)
    replacement, _ = engine.design('A low voice.', 42, language=language, others=[voice])
    assert len(replacement) > len(second)
    assert len(references) == 2
    assert float(references[1].mean()) == pytest.approx(0.2, abs=0.0001)


@pytest.mark.parametrize('language', ['en', 'es'])
def test_fish_clones_the_saved_wav_with_detected_reference_text_and_actual_rate(tmp_path, models, language):
    voice = recording(tmp_path / 'voice.wav', stereo=True, rate=48000)
    engine = speech.Engine(tmp_path / 'hub', 'fish')
    audio, rate = engine.speak('Target passage.', voice, 42, language=language)
    heard = models.transcribed[0]
    assert heard.shape == (7 * 16000,)
    assert float(heard.mean()) == pytest.approx(0.075, abs=0.0001)
    assert models.spoken[0]['audio'].shape == (7 * 44100,)
    assert float(models.spoken[0]['audio'].mean()) == pytest.approx(0.075, abs=0.0001)
    assert models.spoken[0]['ref_text'] == 'Primera frase.'
    assert models.spoken[0]['stream'] is False
    assert rate == 44100 and audio.shape == (22050,) and audio.dtype == np.float32
    assert models.loads == [speech.TRANSCRIBER, speech.PROVIDERS['fish'].models[0]]
    assert models.snapshots[0] == (speech.TRANSCRIBER, speech.TRANSCRIBER_REVISION)
    again, _ = engine.speak('Target passage.', voice, 42, language=language)
    different, _ = engine.speak('Target passage.', voice, 43, language=language)
    np.testing.assert_array_equal(audio, again)
    assert not np.array_equal(audio, different)
    assert len(models.transcribed) == 1


def test_breeze_gets_a_filename_for_the_exact_saved_wav_and_its_transcript(tmp_path, models):
    voice = recording(tmp_path / 'voice.wav', rate=44100)
    engine = speech.Engine(tmp_path / 'hub', 'breeze')
    audio, rate = engine.speak('Hello.', voice, 77)
    assert models.spoken[0] == {'text': 'Hello.', 'wav': voice.read_bytes(), 'ref_text': 'Primera frase.',
                               'seed': 77, 'stream': False}
    assert rate == 24000 and audio.shape == (12000,)
    engine.speak('Again.', voice, 78)
    assert len(models.transcribed) == 1
    assert models.loads == [speech.TRANSCRIBER, speech.PROVIDERS['breeze'].models[0]]


@pytest.mark.parametrize('provider', ['chatterbox', 'fish'])
def test_breeze_directs_an_emotion_take_of_any_saved_voice_whichever_narrator_reads(tmp_path, models, provider):
    voice = recording(tmp_path / 'voice.wav', rate=44100)
    engine = speech.Engine(tmp_path / 'hub', provider)
    audio, rate = engine.direct(voice, 'angry', 9)
    instruction, script = DIRECTIONS['angry']
    assert models.spoken[0] == {'text': script, 'wav': voice.read_bytes(), 'ref_text': 'Primera frase.', 'seed': 9,
                               'stream': False, 'instruct': instruction, 'cfg_scale': 4.0}
    assert rate == 24000 and len(audio) == speech.SAMPLE_SECONDS * rate
    assert models.loads == [speech.TRANSCRIBER, speech.BREEZE]
    engine.direct(voice, 'sad', 9)
    assert models.loads == [speech.TRANSCRIBER, speech.BREEZE], 'the director and the transcript are kept'


def test_the_breeze_narrator_also_directs_takes(tmp_path, models):
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', 'breeze')
    engine.speak('Hello.', voice, 77)
    engine.direct(voice, 'whisper', 9)
    assert models.loads == [speech.TRANSCRIBER, speech.BREEZE]


def test_breeze_rejects_spanish_before_reading_the_reference_or_loading_any_model(tmp_path, models):
    engine = speech.Engine(tmp_path / 'hub', 'breeze')
    with pytest.raises(ValueError, match=r'Breeze.*Spanish.*--tts-provider fish'):
        engine.speak('Hola.', tmp_path / 'missing.wav', 42, language='es')
    assert models.loads == models.snapshots == models.transcribed == models.spoken == []


@pytest.mark.parametrize('provider', ['fish', 'breeze'])
def test_replaced_reference_with_unchanged_path_and_mtime_gets_new_conditioning(tmp_path, models, provider):
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', provider)
    engine.speak('One.', voice, 42)
    before = voice.stat()
    recording(voice, level=0.2)
    os.utime(voice, ns=(before.st_atime_ns, before.st_mtime_ns))
    engine.speak('Two.', voice, 42)
    assert [call['ref_text'] for call in models.spoken] == ['Primera frase.', 'Otra frase.']
    assert len(models.transcribed) == 2
    if provider == 'fish':
        assert float(models.spoken[-1]['audio'].mean()) == pytest.approx(0.2, abs=0.0001)
    else:
        assert models.spoken[-1]['wav'] == voice.read_bytes()


def test_reference_replacement_during_transcription_cannot_poison_the_checksum_cache(tmp_path, models):
    voice = recording(tmp_path / 'voice.wav')
    original = voice.read_bytes()
    engine = speech.Engine(tmp_path / 'hub', 'breeze')
    models.during_transcribe = lambda: recording(voice, level=0.2)
    engine.speak('First.', voice, 42)
    models.during_transcribe = None
    engine.speak('Second.', voice, 42)
    assert models.spoken[0]['wav'] == original
    assert models.spoken[0]['ref_text'] == 'Primera frase.'
    assert models.spoken[1]['wav'] == voice.read_bytes()
    assert models.spoken[1]['ref_text'] == 'Otra frase.'


@pytest.mark.parametrize('provider', ['fish', 'breeze'])
@pytest.mark.parametrize('failure', ['empty', 'failed'])
def test_failed_transcription_never_loads_the_narrator_or_caches_invented_text(tmp_path, models, provider, failure):
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', provider)
    if failure == 'empty':
        models.transcript = '  '
    else:
        models.error = RuntimeError('local STT failed')
    with pytest.raises(RuntimeError, match='Could not transcribe the saved reference WAV locally'):
        engine.speak('Hello.', voice, 42)
    assert models.loads == [speech.TRANSCRIBER]
    assert not models.spoken and not engine.transcripts and not engine.voices
    models.transcript, models.error = None, None
    audio, rate = engine.speak('Hello.', voice, 42)
    assert len(audio) / rate == 0.5
    assert len(models.transcribed) == 2


def test_a_clipped_reference_is_transcribed_instead_of_assigning_the_full_designer_script(tmp_path, models):
    original = recording(tmp_path / 'long.wav', seconds=20)
    clipped, rate = speech.read_sample(original)
    voice = tmp_path / 'saved.wav'
    sf.write(voice, clipped, rate, format='WAV')
    engine = speech.Engine(tmp_path / 'hub', 'fish')
    engine.speak('Narrate this.', voice, 42)
    assert models.transcribed[0].shape == (15 * 16000,)
    assert models.spoken[0]['ref_text'] == 'Primera frase.'
    assert models.spoken[0]['ref_text'] not in [sample[1] for sample in speech.SAMPLE_TEXT.values()]


def test_reference_caches_are_bounded_and_evicted_references_are_transcribed_again(tmp_path, models):
    voice = tmp_path / 'voice.wav'
    engine = speech.Engine(tmp_path / 'hub', 'fish')
    for i in range(speech.CACHE_SIZE + 1):
        recording(voice, level=(i + 1) / 100)
        engine.speak('A line.', voice, 42)
    assert len(engine.transcripts) == len(engine.voices) == speech.CACHE_SIZE
    recording(voice, level=0.01)
    engine.speak('Again.', voice, 42)
    assert len(models.transcribed) == speech.CACHE_SIZE + 2


@pytest.mark.parametrize('provider', ['chatterbox', 'fish', 'breeze'])
@pytest.mark.parametrize('language', ['en', 'es'])
def test_breeze_designs_english_voices_qwen_spanish_and_chatterbox_keeps_them_distinct(tmp_path, models, provider, language):
    other = recording(tmp_path / 'other.wav')
    engine = speech.Engine(tmp_path / 'hub', provider)
    audio, rate = engine.design('A low voice.', 42, language=language, others=[other])
    if language == 'en':
        assert models.loads == [speech.BREEZE, speech.CHATTERBOX]
        assert models.spoken[0] == {'text': speech.SAMPLE_TEXT['en'][1], 'instruct': 'A low voice.', 'cfg_scale': 4.0,
                                    'seed': 42, 'stream': False}
    else:
        assert models.loads == [speech.DESIGNER, speech.MULTILINGUAL]
        assert models.spoken[0] == {'instruction': 'A low voice.', 'design_text': speech.SAMPLE_TEXT['es'][1],
                                    'language': 'Spanish'}
    assert rate == 24000 and len(audio) == 15 * rate
    assert models.transcribed == []


@pytest.mark.parametrize('provider', ['chatterbox', 'breeze'])
def test_one_breeze_model_designs_directs_and_narrates(tmp_path, models, provider):
    engine = speech.Engine(tmp_path / 'hub', provider)
    voice = tmp_path / 'voice.wav'
    sf.write(voice, *engine.design('A low voice.', 42), format='WAV')
    engine.direct(voice, 'angry', 9)
    engine.speak('Hello.', voice, 77)
    assert models.loads.count(speech.BREEZE) == 1


def test_a_male_voice_above_its_ceiling_is_designed_again_with_the_next_seed(tmp_path, models, monkeypatch):
    pitches = iter([300., 240., 190., 100.])
    monkeypatch.setattr(speech, 'median_pitch', lambda audio, rate: next(pitches))
    engine = speech.Engine(tmp_path / 'hub', 'fish')
    audio, rate = engine.design('A young man.', 42, ceiling=210)
    assert [call['seed'] for call in models.spoken] == [42, 43, 44]
    third, _ = engine.design('A young man.', 44)
    np.testing.assert_array_equal(audio, third)


@pytest.mark.parametrize('hz', [120, 300])
def test_median_pitch_finds_the_fundamental_of_a_voiced_tone(hz):
    rate = 24000
    t = np.arange(2 * rate) / rate
    tone = sum(0.3 / k * np.sin(2 * np.pi * hz * k * t) for k in (1, 2, 3)).astype('float32')
    assert speech.median_pitch(tone, rate) == pytest.approx(hz, rel=0.05)
    assert speech.median_pitch(np.zeros(2 * rate, dtype='float32'), rate) is None


def test_the_first_sample_within_the_ceiling_is_kept_and_no_more_are_made():
    made = []

    def make(i):
        made.append(i)
        return f'sample {i}', [300., 205., 100., 90.][i]
    assert speech.within_ceiling(4, make, 210) == 'sample 1'
    assert made == [0, 1]


def test_the_lowest_sample_is_kept_when_none_is_within_the_ceiling():
    made = []

    def make(i):
        made.append(i)
        return f'sample {i}', [300., 260., None, 280.][i]
    assert speech.within_ceiling(4, make, 210) == 'sample 1'
    assert made == [0, 1, 2, 3]


def test_without_a_ceiling_one_sample_is_made():
    made = []

    def make(i):
        made.append(i)
        return f'sample {i}', None
    assert speech.within_ceiling(4, make, None) == 'sample 0'
    assert made == [0]


@pytest.mark.parametrize('age', ['teen', 'adult', 'elderly'])
@pytest.mark.parametrize('pitch, hz', [('low', 160), ('medium', 210), ('high', 250)])
def test_men_and_older_boys_get_a_pitch_ceiling(age, pitch, hz):
    assert speech.pitch_ceiling({'gender': 'male', 'age': age, 'pitch': pitch}) == hz


@pytest.mark.parametrize('gender, age', [('male', 'child'), ('female', 'adult'), ('female', 'teen'),
                                         ('unknown', 'adult'), ('unknown', 'teen')])
def test_children_women_and_unknown_speakers_get_no_ceiling(gender, age):
    assert speech.pitch_ceiling({'gender': gender, 'age': age, 'pitch': 'medium'}) is None


def test_a_teenage_boy_is_voiced_as_a_young_man_of_eighteen():
    teen = {'gender': 'male', 'age': 'teen', 'pitch': 'medium', 'description': 'quick and animated'}
    assert speech.voice_instruction(teen) == 'A young man of about eighteen with a medium-pitched voice. Quick and animated.'


def test_whisper_is_released_after_transcription_and_cached_calls_do_not_reload_it(tmp_path, models, monkeypatch):
    import weakref
    import mlx_audio.stt

    loaded = []
    load = mlx_audio.stt.load

    def track(repo):
        model = load(repo)
        loaded.append(weakref.ref(model))
        return model
    monkeypatch.setattr(mlx_audio.stt, 'load', track)
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', 'fish')
    engine.speak('First.', voice, 42)
    assert len(loaded) == 1 and loaded[0]() is None
    engine.speak('Second.', voice, 43)
    assert len(loaded) == 1 and len(models.transcribed) == 1
    recording(voice, level=0.2)
    engine.speak('Replacement.', voice, 44)
    assert len(loaded) == 2 and all(reference() is None for reference in loaded)


def test_a_book_in_another_language_still_gets_a_designed_voice_with_the_english_sample(tmp_path, models):
    engine = speech.Engine(tmp_path / 'hub', 'chatterbox')
    engine.design('A voice.', 42, language='fr')
    assert models.spoken[0] == {'instruction': 'A voice.', 'design_text': speech.SAMPLE_TEXT['en'][1],
                                'language': speech.SAMPLE_TEXT['en'][0]}


@pytest.mark.parametrize('language', ['fr', 'de', 'es'])
@pytest.mark.parametrize('provider', ['chatterbox', 'fish'])
def test_chatterbox_and_fish_narrate_any_language_their_model_accepts(provider, language):
    speech.PROVIDERS[provider].validate_language(language)


@pytest.mark.parametrize('provider', ['breeze', 'breeze-gguf'])
def test_breeze_refuses_non_english_narration_with_the_provider_to_switch_to(provider):
    with pytest.raises(ValueError, match='narrates English only, and this book is in fr'):
        speech.PROVIDERS[provider].validate_language('fr')
    with pytest.raises(ValueError, match='For Spanish, restart with --tts-provider chatterbox'):
        speech.PROVIDERS[provider].validate_language('es')


def test_generation_result_rate_controls_the_returned_audio_not_the_model_default(tmp_path, models):
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', 'fish')
    models.result_rate = 32000
    audio, rate = engine.speak('Hello.', voice, 42)
    assert rate == 32000 and len(audio) == 16000


@pytest.mark.parametrize('results, message', [
    ([], 'no audio'),
    ([SimpleNamespace(audio=mx.zeros((2, 10)), sample_rate=24000)], 'invalid mono'),
    ([SimpleNamespace(audio=mx.array([float('nan')]), sample_rate=24000)], 'invalid mono'),
    ([SimpleNamespace(audio=mx.array([float('inf')]), sample_rate=24000)], 'invalid mono'),
    ([SimpleNamespace(audio=np.full(10, 1e100), sample_rate=24000)], 'invalid mono'),
    ([SimpleNamespace(audio=mx.array(0.1), sample_rate=24000)], 'invalid mono'),
    ([SimpleNamespace(audio=mx.zeros((2, 1)), sample_rate=24000)], 'invalid mono'),
    ([SimpleNamespace(audio=mx.ones((1, 1, 10)), sample_rate=24000)], 'invalid mono'),
    ([SimpleNamespace(audio=mx.zeros(0), sample_rate=24000)], 'no audio'),
    ([SimpleNamespace(audio=mx.ones(10), sample_rate=24000),
      SimpleNamespace(audio=mx.zeros(0), sample_rate=24000)], 'no audio'),
    ([SimpleNamespace(audio=mx.ones(10), sample_rate=24000),
      SimpleNamespace(audio=mx.ones(10), sample_rate=44100)], 'inconsistent sample rates'),
])
def test_invalid_model_output_is_not_flattened_or_published(tmp_path, results, message):
    engine = speech.Engine(tmp_path / 'hub')
    with pytest.raises(RuntimeError, match=message):
        engine._generate(results, 24000)


@pytest.mark.parametrize('rate', [0, -1, 24000.5, float('nan'), float('inf'), True, '24000', None])
def test_invalid_actual_sample_rate_is_rejected(tmp_path, rate):
    engine = speech.Engine(tmp_path / 'hub')
    results = [SimpleNamespace(audio=mx.ones(10), sample_rate=rate)]
    with pytest.raises(RuntimeError, match='invalid sample rate'):
        engine._generate(results, 24000)


@pytest.mark.parametrize('rate', [24000, 24000.0, np.int32(24000), np.float64(24000)])
def test_integral_actual_rate_returns_an_integer_and_mono_float32(tmp_path, rate):
    engine = speech.Engine(tmp_path / 'hub')
    audio, actual = engine._generate([SimpleNamespace(audio=mx.ones((1, 10)), sample_rate=rate)], 44100)
    assert type(actual) is int and actual == 24000
    assert audio.shape == (10,) and audio.dtype == np.float32
    assert np.isfinite(audio).all()


@pytest.mark.parametrize('provider', ['fish', 'breeze'])
def test_upload_and_candidate_approval_condition_on_the_new_saved_wav(world, tmp_path, models, provider):
    from test_pipeline import focus

    focus(world, 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    narrator = world.store.narrator(world.book['id'])
    voice_url = f"/api/characters/{narrator['id']}/voice"
    assert world.client.post(f'{voice_url}/draft').status_code == 200
    assert world.pipeline.speech_step()
    recording(world.store.draft_path(narrator), seconds=8, level=0.4)
    world.pipeline.engine = speech.Engine(tmp_path / 'hub', provider)
    models.transcript = 'The original voice says this.'
    assert world.pipeline.speech_step()
    original = world.store.voice_path(narrator).read_bytes()
    models.transcript = 'The clipped upload ends here.'
    upload = recording(tmp_path / 'upload.wav', seconds=20, rate=32000, level=0.3)
    assert world.client.post(voice_url, files={'file': ('upload.wav', upload.read_bytes(), 'audio/wav')}).status_code == 200
    assert world.pipeline.speech_step()
    saved_upload = world.store.voice_path(narrator).read_bytes()
    assert original != saved_upload
    assert models.transcribed[-1].shape == (15 * 16000,)
    assert float(models.transcribed[-1].mean()) == pytest.approx(0.3, abs=0.001)
    uploaded = models.spoken[-1]
    assert uploaded['ref_text'] == 'The clipped upload ends here.'
    if provider == 'fish':
        assert uploaded['audio'].shape == (15 * 44100,)
        assert float(uploaded['audio'].mean()) == pytest.approx(0.3, abs=0.001)
    else:
        assert uploaded['wav'] == saved_upload
    models.transcript = 'The approved candidate says this.'
    candidate = world.store.draft_path(narrator).read_bytes()
    assert world.client.post(f'{voice_url}/draft/approve').status_code == 200
    assert world.pipeline.speech_step()
    assert world.store.voice_path(narrator).read_bytes() == candidate
    assert candidate != saved_upload
    approved = models.spoken[-1]
    assert approved['ref_text'] == 'The approved candidate says this.'
    assert models.transcribed[-1].shape == (8 * 16000,)
    assert float(models.transcribed[-1].mean()) == pytest.approx(0.4, abs=0.001)
    if provider == 'fish':
        assert approved['audio'].shape == (8 * 44100,)
        assert float(approved['audio'].mean()) == pytest.approx(0.4, abs=0.001)
    else:
        assert approved['wav'] == candidate
    assert len(models.transcribed) == 3
    assert world.pipeline.speech_step() is False
    line = world.chapter()[0]['segments'][0]
    assert (line['audio_state'], line['duration']) == ('ready', 0.5)
    info = sf.info(world.store.audio_path(world.book['id'], line['id']))
    assert info.samplerate == (44100 if provider == 'fish' else 24000) and info.subtype == 'PCM_16'


@pytest.mark.parametrize('provider, rate', [('fish', 44100), ('breeze', 24000)])
def test_selected_engine_lands_playable_pcm16_flac_at_the_reported_rate(world, tmp_path, models, provider, rate):
    import io

    voice = recording(tmp_path / 'upload.wav', seconds=20, rate=48000)
    narrator = world.store.narrator(world.book['id'])
    reply = world.client.post(f"/api/characters/{narrator['id']}/voice",
                              files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')})
    assert reply.status_code == 200
    saved = world.store.voice_path(narrator).read_bytes()
    world.pipeline.engine = speech.Engine(tmp_path / 'hub', provider)
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    clip = world.store.chapter_blocks(world.book['id'], 0)[0]['segments'][0]
    response = world.client.get(f"/api/segments/{clip['id']}/audio")
    assert response.status_code == 200
    info = sf.info(io.BytesIO(response.content))
    samples, decoded_rate = sf.read(io.BytesIO(response.content), dtype='float32')
    assert (info.format, info.subtype, info.channels, decoded_rate) == ('FLAC', 'PCM_16', 1, rate)
    assert info.frames == int(rate * 0.5) and samples.shape == (info.frames,)
    assert np.isfinite(samples).all() and float(samples.mean()) > 0.09
    assert (clip['audio_state'], clip['duration']) == ('ready', 0.5)
    assert models.transcribed[0].shape == (15 * 16000,)
    assert world.store.voice_path(narrator).read_bytes() == saved


@pytest.mark.parametrize('result', [
    SimpleNamespace(audio=mx.full((2, 10), 0.1), sample_rate=44100),
    SimpleNamespace(audio=mx.full(10, float('nan')), sample_rate=44100),
    SimpleNamespace(audio=mx.full(10, 0.1), sample_rate=44100.5),
    SimpleNamespace(audio=mx.full(10, 0.1), sample_rate=float('inf')),
    SimpleNamespace(audio=mx.zeros(0), sample_rate=44100),
])
def test_invalid_narration_never_lands_a_flac(world, tmp_path, models, result):
    narrator = world.store.narrator(world.book['id'])
    voice = recording(tmp_path / 'upload.wav')
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    engine = speech.Engine(tmp_path / 'hub', 'fish')
    engine.narrator = SimpleNamespace(sample_rate=44100, generate=lambda **kwargs: iter([result]))
    world.pipeline.engine = engine
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    assert not world.pipeline.speech_step()
    clip = world.chapter()[0]['segments'][0]
    assert clip['audio_state'] == 'failed' and clip['duration'] is None
    assert 'speech model returned' in clip['error']
    assert not world.store.audio_path(world.book['id'], clip['id']).exists()
    assert world.client.get(f"/api/segments/{clip['id']}/audio").status_code == 404
    assert world.store.character(narrator['id'])['voice_state'] == 'ready'


@pytest.mark.parametrize('approved', [False, True])
def test_breeze_rejection_preserves_cast_and_fish_restart_recovers(world, tmp_path, models, approved):
    from fastapi.testclient import TestClient
    from meread.app import create_app

    book_id = world.book['id']
    narrator = world.store.narrator(book_id)
    if approved:
        voice = recording(tmp_path / 'upload.wav')
        assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                                 files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    world.store.add_character(book_id, 'Missing voice', 'named')
    world.store.run("UPDATE book SET language = 'es' WHERE id = ?", (book_id,))
    before = world.store.characters(book_id)
    saved = {world.store.voice_path(c): world.store.voice_path(c).read_bytes()
             for c in before if world.store.voice_path(c).exists()}
    breeze = create_app(tmp_path, engine=speech.Engine(tmp_path / 'hub', 'breeze'), run_workers=False)
    with TestClient(breeze) as client:
        pipeline, store = breeze.state.pipeline, breeze.state.store
        events = []
        pipeline.publish = lambda book, event: events.append(event)
        pipeline.set_focus(book_id, 0, 1, 0, 1)
        assert pipeline.speech_step()
        assert not pipeline.speech_step()
        assert store.characters(book_id) == before
        assert {path: path.read_bytes() for path in saved} == saved
        clips = [s for b in store.chapter_blocks(book_id, 0)[:2] for s in b['segments']]
        assert all(s['audio_state'] == 'failed' and '--tts-provider fish' in s['error'] for s in clips)
        assert 'Spanish' in pipeline.idle['speech']
        assert any(event['type'] == 'segments' for event in events)
        assert not any(event['type'] == 'characters' for event in events)
        assert client.get(f"/api/segments/{clips[0]['id']}/audio").status_code == 404
    assert models.loads == models.snapshots == models.transcribed == models.spoken == []

    fish = create_app(tmp_path, engine=speech.Engine(tmp_path / 'hub', 'fish'), run_workers=False)
    with TestClient(fish) as client:
        pipeline, store = fish.state.pipeline, fish.state.store
        assert store.characters(book_id) == before
        for clip in clips:
            after = store.segment(clip['id'])
            assert (after['audio_state'], after['audio_rev'], after['error']) == ('none', clip['audio_rev'] + 1, None)
        pipeline.set_focus(book_id, 0, 1, 0, 1)
        for _ in range(10):
            if not pipeline.speech_step():
                break
        else:
            pytest.fail('Fish did not finish the focused jobs')
        assert all(c['voice_state'] == 'ready' for c in store.characters(book_id))
        assert all(store.segment(clip['id'])['audio_state'] == 'ready' for clip in clips)
        assert client.get(f"/api/segments/{clips[0]['id']}/audio").status_code == 200
        assert {path: path.read_bytes() for path in saved} == saved
    assert speech.DESIGNER in models.loads
    assert speech.PROVIDERS['fish'].models[0] in models.loads
    assert speech.PROVIDERS['breeze'].models[0] not in models.loads
    assert all(call['language'] == 'Spanish' for call in models.spoken if 'design_text' in call)


def test_queued_spanish_candidate_under_breeze_remains_approvable_and_reusable_for_fish(world, tmp_path, models):
    from fastapi.testclient import TestClient
    from meread.app import create_app

    book_id = world.book['id']
    narrator = world.store.narrator(book_id)
    voice = recording(tmp_path / 'upload.wav')
    url = f"/api/characters/{narrator['id']}/voice"
    assert world.client.post(url, files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    world.store.run("UPDATE book SET language = 'es' WHERE id = ?", (book_id,))
    approved = world.store.voice_path(narrator).read_bytes()
    before = world.store.character(narrator['id'])
    breeze = create_app(tmp_path, engine=speech.Engine(tmp_path / 'hub', 'breeze'), run_workers=False)
    with TestClient(breeze) as client:
        pipeline, store = breeze.state.pipeline, breeze.state.store
        pipeline.set_focus(book_id, 0, 0, 0, 0)
        assert client.post(f'{url}/draft').json()['draft_state'] == 'queued'
        assert models.loads == []
        assert pipeline.speech_step()
        candidate = store.character(narrator['id'])
        assert (candidate['voice_state'], candidate['voice_rev'], candidate['error']) == (
            'ready', before['voice_rev'], None)
        assert candidate['draft_state'] == 'ready'
        assert store.voice_path(narrator).read_bytes() == approved
        audition = client.get(f'{url}/draft')
        assert audition.status_code == 200
        assert models.loads == [speech.DESIGNER]
        assert models.spoken[0]['language'] == 'Spanish'
        assert pipeline.speech_step()
        assert not pipeline.speech_step()
        assert store.character(narrator['id']) == candidate
        assert client.post(f'{url}/draft/approve').status_code == 200
        assert store.voice_path(narrator).read_bytes() == audition.content
        assert pipeline.speech_step()
        assert not pipeline.speech_step()
        approved_cast = store.characters(book_id)
    assert models.loads == [speech.DESIGNER]
    assert models.transcribed == []

    fish = create_app(tmp_path, engine=speech.Engine(tmp_path / 'hub', 'fish'), run_workers=False)
    with TestClient(fish):
        pipeline, store = fish.state.pipeline, fish.state.store
        assert store.characters(book_id) == approved_cast
        pipeline.set_focus(book_id, 0, 0, 0, 0)
        assert pipeline.speech_step()
        assert not pipeline.speech_step()
        assert store.voice_path(narrator).read_bytes() == audition.content
        assert store.chapter_blocks(book_id, 0)[0]['segments'][0]['audio_state'] == 'ready'
    assert models.loads == [speech.DESIGNER, speech.TRANSCRIBER, speech.PROVIDERS['fish'].models[0]]


def test_spanish_breeze_job_fails_with_an_actionable_error_and_no_model_load(world, tmp_path, models):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    world.client.post(f"/api/characters/{narrator['id']}/voice",
                      files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')})
    world.store.run("UPDATE book SET language = 'es' WHERE id = ?", (world.book['id'],))
    world.pipeline.engine = speech.Engine(tmp_path / 'hub', 'breeze')
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    clip = world.store.chapter_blocks(world.book['id'], 0)[0]['segments'][0]
    assert clip['audio_state'] == 'failed'
    assert 'Breeze' in clip['error'] and 'Spanish' in clip['error'] and '--tts-provider fish' in clip['error']
    assert models.loads == models.snapshots == []
    assert world.client.get(f"/api/segments/{clip['id']}/audio").status_code == 404
