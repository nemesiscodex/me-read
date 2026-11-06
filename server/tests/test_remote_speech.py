import base64
import email.parser
import email.policy
import functools
import hashlib
import io
import json
import socket
import threading
import time
import types

import numpy as np
import pytest
import soundfile as sf
import uvicorn
from fastapi import Response

import server as speech_service
from meread import jobs, speech
from meread import pipeline as pipeline_module
from meread.emotions import DIRECTIONS
from test_speech import models, recording, speech_with_pauses  # noqa: F401  the local stubs the remote engine must never reach

CONSENT = 'I own these reference voices and allow them to be cloned for my own books.'
# The speech service's engines. A model whose engine is missing answers 400 model_not_installed.
ENGINES = ('breeze', 'qwen', 'chatterbox-turbo', 'chatterbox-multilingual', 'audio8', 'qwen-base')


def name_for(wav):
    return 'mr-' + hashlib.sha256(wav).hexdigest()[:32]


def wav_bytes(rate=24000, seconds=0.5, level=0.25, channels=1, subtype='PCM_16'):
    buffer = io.BytesIO()
    sf.write(buffer, np.full((int(rate * seconds), channels), level, dtype='float32'), rate, format='WAV', subtype=subtype)
    return buffer.getvalue()


def fields_of(content_type, body):
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    fields = {}
    for part in message.iter_parts():
        payload = part.get_payload(decode=True)
        fields[part.get_param('name', header='content-disposition')] = payload if part.get_filename() else payload.decode()
    return fields


class Tone:
    """A loaded engine of the speech service that answers a tone: 20 seconds for a design or a take, half a second for a
    line. The SpeechServer it belongs to sets what it answers."""

    def __init__(self, server):
        self.server = server

    def alive(self):
        return True

    def stop(self):
        pass

    def speak(self, request, voice):
        server = self.server
        time.sleep(server.delay)
        if server.failure:
            raise RuntimeError(server.failure)
        wav = server.replies.pop(0) if server.replies else wav_bytes(server.rate, seconds=20 if request.instructions else 0.5)
        return wav, server.disclaimer if voice else server.design_disclaimer

    def speaker(self, audio, rate):
        return self.server.embed(audio, rate)


class SpeechServer:
    """The GPU speech service's own app with Tone engines, served over HTTP on this machine. It records every request.
    `canned` answers a path with a fixed (status, body, content type, headers) instead, for replies that only something
    in front of the service, such as a proxy, could send."""

    def __init__(self, folder, port=0):
        self.folder, self.calls, self.canned = folder, [], {}
        self.rate, self.replies, self.failure, self.delay = 24000, [], None, 0
        self.disclaimer, self.design_disclaimer = 'skipped', None
        self.embed = lambda audio, rate: [1., 0.]
        self.gates = {name: threading.Event() for name in ENGINES}
        self.release()
        self.loader = speech_service.Loader({name: speech_service.Engine(1, functools.partial(self._load, name))
                                             for name in ENGINES}, budget=len(ENGINES), wait=0.5)
        self.app = speech_service.create_app(self.loader, speech_service.Voices(folder))
        sock = socket.socket()
        sock.bind(('127.0.0.1', port))
        self.url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        self.server = uvicorn.Server(uvicorn.Config(self._record, interface='asgi3', log_config=None, access_log=False))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        self.thread.start()
        while not self.server.started:
            assert self.thread.is_alive(), 'the speech service did not start'
            time.sleep(0.01)

    def _load(self, name):
        self.gates[name].wait()
        return Tone(self)

    def hold(self, *names):
        """Keeps these engines loading until `release`, so the service answers their requests 503 loading."""
        for name in names:
            self.gates[name].clear()

    def release(self):
        for gate in self.gates.values():
            gate.set()

    async def _record(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        body, more = b'', True
        while more:
            message = await receive()
            body, more = body + message.get('body', b''), message.get('more_body', False)
        self.calls.append((scope['path'], dict(scope['headers']).get(b'content-type', b'').decode(), body))
        if scope['path'] in self.canned:
            status, reply, content_type, headers = self.canned[scope['path']]
            return await Response(reply, status, headers, content_type)(scope, receive, send)
        unread = [{'type': 'http.request', 'body': body, 'more_body': False}]

        async def replay():
            return unread.pop() if unread else await receive()
        await self.app(scope, replay, send)

    def _bodies(self, path):
        return [(content_type, body) for called, content_type, body in self.calls if called == path]

    @property
    def paths(self):
        return [path for path, _, _ in self.calls]

    @property
    def uploads(self):
        return [fields_of(content_type, body) for content_type, body in self._bodies('/v1/voices')]

    @property
    def speeches(self):
        return [json.loads(body) for _, body in self._bodies('/v1/audio/speech')]

    @property
    def heard(self):
        heard = []
        for _, body in self._bodies('/v1/speaker'):
            request = json.loads(body)
            audio, rate = sf.read(io.BytesIO(base64.b64decode(request['audio'])), dtype='float32')
            heard.append({'seconds': len(audio) / rate, 'level': float(audio.mean()), 'language': request['language']})
        return heard

    @property
    def voices(self):
        """The voices saved on the service, by name."""
        return {path.stem: path.read_bytes() for path in self.folder.glob('*.wav')}

    def forget_voices(self):
        for path in self.folder.glob('*.wav'):
            path.unlink()

    def close(self):
        self.release()
        self.server.should_exit = True
        self.thread.join()


@pytest.fixture
def speech_server(tmp_path):
    server = SpeechServer(tmp_path / 'service-voices')
    yield server
    server.close()


@pytest.fixture
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail('a network call was made')
    monkeypatch.setattr(speech.urllib.request, 'urlopen', refused)


def remote_engine(tmp_path, server):
    return speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=server.url, tts_consent=CONSENT)


def test_each_reference_is_uploaded_once_per_content_hash_and_reused(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    engine = remote_engine(tmp_path, speech_server)
    engine.speak('One.', voice, 42)
    engine.speak('Two.', voice, 43)
    wav = voice.read_bytes()
    assert [upload['name'] for upload in speech_server.uploads] == [name_for(wav)]
    assert speech_server.uploads[0]['voice'] == wav
    assert [call['voice'] for call in speech_server.speeches] == [name_for(wav)] * 2
    assert speech_server.voices == {name_for(wav): wav}
    assert len(models.transcribed) == 1
    assert models.loads == [speech.TRANSCRIBER]


def test_replaced_reference_under_the_same_filename_gets_a_new_voice_name(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    original = voice.read_bytes()
    engine = remote_engine(tmp_path, speech_server)
    engine.speak('One.', voice, 42)
    recording(voice, level=0.2)
    replacement = voice.read_bytes()
    engine.speak('Two.', voice, 42)
    assert name_for(original) != name_for(replacement)
    assert [call['voice'] for call in speech_server.speeches] == [name_for(original), name_for(replacement)]
    assert speech_server.voices == {name_for(original): original, name_for(replacement): replacement}


def test_speech_request_carries_transcript_consent_seed_and_disclaimer_off(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav', rate=44100)
    models.transcript = 'The saved words.'
    engine = remote_engine(tmp_path, speech_server)
    engine.speak('Hello there.', voice, 77)
    assert speech_server.speeches == [{
        'model': 'breeze', 'input': 'Hello there.', 'voice': name_for(voice.read_bytes()), 'ref_text': 'The saved words.', 'seed': 77,
        'response_format': 'wav', 'consent_attestation': CONSENT, 'spoken_disclaimer': False}]
    assert speech_server.uploads == [{'voice': voice.read_bytes(), 'name': name_for(voice.read_bytes()),
                                      'consent_attestation': CONSENT}]
    assert float(models.transcribed[0].mean()) == pytest.approx(0.1, abs=0.0001)


@pytest.mark.parametrize('rate', [24000, 44100])
def test_returned_wav_becomes_mono_float32_at_the_rate_the_server_reports(tmp_path, models, speech_server, rate):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.rate = rate
    audio, actual = remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert actual == rate and audio.dtype == np.float32 and audio.ndim == 1
    assert len(audio) == rate // 2
    assert float(audio.mean()) == pytest.approx(0.25, abs=0.0001)


def test_spanish_is_rejected_before_any_file_read_or_network_call(tmp_path, models, no_network):
    engine = speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url='http://127.0.0.1:9', tts_consent=CONSENT)
    with pytest.raises(ValueError, match=r'Breeze-gguf.*Spanish.*--tts-provider fish'):
        engine.speak('Hola.', tmp_path / 'missing.wav', 42, language='es')
    assert models.loads == models.snapshots == models.transcribed == models.spoken == []


@pytest.mark.parametrize('provider', ['chatterbox', 'fish', 'breeze'])
def test_local_providers_never_touch_the_speech_server(tmp_path, models, no_network, provider):
    voice = recording(tmp_path / 'voice.wav')
    audio, rate = speech.Engine(tmp_path / 'hub', provider).speak('Hello.', voice, 42)
    assert rate and len(audio)


def test_an_upload_the_service_refuses_stops_before_any_speech_request(tmp_path, models, speech_server):
    voice = tmp_path / 'voice.wav'
    voice.write_bytes(b'not a wav')
    with pytest.raises(speech.RemoteError, match=r'HTTP 400 invalid_audio') as refused:
        gpu_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert CONSENT not in str(refused.value) and not isinstance(refused.value, speech.Unavailable)
    assert speech_server.speeches == [] and speech_server.voices == {}


def test_server_error_on_speech_is_reported_with_its_code_and_nothing_else(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.failure = 'out of memory'
    with pytest.raises(RuntimeError, match=r'HTTP 500 synthesis_failed: out of memory') as failed:
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert CONSENT not in str(failed.value) and 'Hello' not in str(failed.value)


def test_server_still_loading_is_reported_as_http_503(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.hold('breeze')
    with pytest.raises(speech.Unavailable, match='HTTP 503 loading'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def test_an_existing_name_on_upload_means_the_voice_is_already_there(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    remote_engine(tmp_path, speech_server).speak('First.', voice, 42)
    audio, rate = remote_engine(tmp_path, speech_server).speak('Second.', voice, 42)
    assert len(speech_server.uploads) == 2 and len(audio) == rate // 2
    assert [call['voice'] for call in speech_server.speeches] == [name_for(voice.read_bytes())] * 2


def test_unreachable_server_is_reported_without_the_consent_text(tmp_path, models):
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=f'http://127.0.0.1:{speech_service.free_port()}',
                           tts_consent=CONSENT)
    with pytest.raises(speech.Unavailable, match='Could not reach the speech server at 127.0.0.1') as unreachable:
        engine.speak('Hello.', voice, 42)
    assert CONSENT not in str(unreachable.value)


def test_slow_speech_request_times_out_with_a_clear_error(tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'voice.wav')
    monkeypatch.setattr(speech.Remote, 'SPEECH_TIMEOUT', 0.1)
    speech_server.delay = 0.5
    with pytest.raises(speech.Unavailable, match='did not answer within 0.1 seconds'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


@pytest.mark.parametrize('reply, message', [
    (wav_bytes(seconds=0), 'no audio'),
    (wav_bytes(level=float('nan'), subtype='FLOAT'), 'invalid mono'),
    (wav_bytes(channels=2), 'invalid mono'),
])
def test_unusable_wav_from_the_server_never_becomes_audio(tmp_path, models, speech_server, reply, message):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.replies = [reply]
    with pytest.raises(RuntimeError, match=message):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def test_a_speech_reply_that_is_not_a_wav_never_becomes_audio(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.canned['/v1/audio/speech'] = (200, b'<html>not audio</html>', 'text/html', {speech.Remote.DISCLAIMER: 'skipped'})
    with pytest.raises(RuntimeError, match='not a readable WAV'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def test_forgotten_voice_is_uploaded_again_once_and_the_request_retried_once(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    engine = remote_engine(tmp_path, speech_server)
    engine.speak('One.', voice, 42)
    speech_server.forget_voices()
    audio, rate = engine.speak('Two.', voice, 42)
    assert len(speech_server.uploads) == 2 and len(speech_server.speeches) == 3
    assert speech_server.voices == {name_for(voice.read_bytes()): voice.read_bytes()} and len(audio) == rate // 2


def test_a_server_that_keeps_failing_gets_exactly_one_retry_not_a_loop(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    engine = remote_engine(tmp_path, speech_server)
    engine.speak('One.', voice, 42)
    speech_server.forget_voices()
    speech_server.failure = 'out of memory'
    uploads, speeches = len(speech_server.uploads), len(speech_server.speeches)
    with pytest.raises(RuntimeError, match='synthesis_failed'):
        engine.speak('Two.', voice, 42)
    assert len(speech_server.uploads) - uploads == 1
    assert len(speech_server.speeches) - speeches == 2


def test_a_fresh_upload_that_is_followed_by_a_failed_speech_is_not_retried(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.failure = 'out of memory'
    with pytest.raises(RuntimeError, match='synthesis_failed'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert len(speech_server.uploads) == 1 and len(speech_server.speeches) == 1


@pytest.mark.parametrize('url', ['ftp://127.0.0.1:8781', 'http://user:secret@127.0.0.1:8781', 'http://', 'http://127.0.0.1:8781?x=1', None, ''])
def test_addresses_that_are_not_plain_http_without_a_login_are_refused_without_echoing_them(url):
    with pytest.raises(ValueError) as refused:
        speech.Remote(url, CONSENT)
    assert 'secret' not in str(refused.value) and 'user' not in str(refused.value)


@pytest.mark.parametrize('consent', [None, '', '   '])
def test_consent_is_required_and_has_no_default(consent):
    with pytest.raises(ValueError, match='--tts-consent is required'):
        speech.Remote('http://127.0.0.1:8781', consent)


def test_consent_that_is_not_utf8_text_is_refused_when_the_client_is_built():
    with pytest.raises(ValueError, match='plain UTF-8 text'):
        speech.Remote('http://127.0.0.1:8781', 'I allow this \udc80 for my books.')


def test_one_trailing_slash_is_stripped_from_the_address(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=speech_server.url + '/', tts_consent=CONSENT)
    engine.speak('Hello.', voice, 42)
    assert speech_server.paths == ['/v1/voices', '/v1/audio/speech']


def test_remote_narration_lands_a_flac_at_the_server_rate(world, tmp_path, models, speech_server):
    voice = recording(tmp_path / 'upload.wav', seconds=20, rate=48000)
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    saved = world.store.voice_path(narrator).read_bytes()
    models.transcript = 'The saved narrator says this.'
    world.pipeline.engine = remote_engine(tmp_path, speech_server)
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    clip = world.store.chapter_blocks(world.book['id'], 0)[0]['segments'][0]
    assert (clip['audio_state'], clip['duration']) == ('ready', 0.5)
    assert speech_server.voices == {name_for(saved): saved}
    assert speech_server.speeches[0]['ref_text'] == 'The saved narrator says this.'
    response = world.client.get(f"/api/segments/{clip['id']}/audio")
    info = sf.info(io.BytesIO(response.content))
    assert (info.format, info.subtype, info.channels, info.samplerate, info.frames) == ('FLAC', 'PCM_16', 1, 24000, 12000)


def test_failed_remote_narration_lands_no_clip_and_keeps_the_cast(world, tmp_path, models, speech_server):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    speech_server.failure = 'out of memory'
    world.pipeline.engine = remote_engine(tmp_path, speech_server)
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    assert not world.pipeline.speech_step()
    clip = world.chapter()[0]['segments'][0]
    assert clip['audio_state'] == 'failed' and clip['duration'] is None
    assert 'HTTP 500 synthesis_failed: out of memory' in clip['error'] and CONSENT not in clip['error']
    assert not world.store.audio_path(world.book['id'], clip['id']).exists()
    assert world.client.get(f"/api/segments/{clip['id']}/audio").status_code == 404
    assert world.store.character(narrator['id'])['voice_state'] == 'ready'


def test_spanish_book_under_breeze_gguf_fails_before_any_upload_or_model_load(world, tmp_path, models, speech_server):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    world.client.post(f"/api/characters/{narrator['id']}/voice", files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')})
    world.store.run("UPDATE book SET language = 'es' WHERE id = ?", (world.book['id'],))
    world.pipeline.engine = remote_engine(tmp_path, speech_server)
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    clip = world.chapter()[0]['segments'][0]
    assert clip['audio_state'] == 'failed'
    assert 'Breeze-gguf' in clip['error'] and 'Spanish' in clip['error'] and '--tts-provider fish' in clip['error']
    assert speech_server.paths == [] and models.loads == models.snapshots == models.transcribed == []


def test_a_speech_server_outage_waits_then_retries_and_no_clip_fails(world, tmp_path, models, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pipeline_module, 'time', types.SimpleNamespace(monotonic=lambda: clock[0]))
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    models.transcript = 'The saved narrator says this.'
    port, running, states = speech_service.free_port(), None, []
    world.pipeline.engine = speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=f'http://127.0.0.1:{port}', tts_consent=CONSENT)
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)

    def clip():
        state = world.chapter()[0]['segments'][0]
        states.append(state['audio_state'])
        return state
    try:
        assert world.pipeline.speech_step()
        assert clip()['audio_state'] == 'none'
        assert 'unavailable' in world.pipeline.idle['speech'] and world.pipeline.retry_at['speech'] == 1015
        clock[0] = 1005
        assert not world.pipeline.speech_step()
        assert 'unavailable' in world.pipeline.idle['speech']
        running = SpeechServer(tmp_path / 'service-voices', port)
        running.hold('breeze')
        clock[0] = 1015
        assert world.pipeline.speech_step()
        assert clip()['audio_state'] == 'none' and running.paths == ['/v1/voices', '/v1/audio/speech']
        assert world.pipeline.retry_at['speech'] == 1030
        clock[0] = 1029
        assert not world.pipeline.speech_step()
        assert running.paths == ['/v1/voices', '/v1/audio/speech']
        running.release()
        clock[0] = 1030
        assert world.pipeline.speech_step()
        ready = clip()
        assert (ready['audio_state'], ready['duration']) == ('ready', 0.5)
        assert world.pipeline.idle['speech'] is None
        assert 'failed' not in states
        assert running.paths == ['/v1/voices', '/v1/audio/speech', '/v1/audio/speech']
        assert running.speeches[0]['ref_text'] == 'The saved narrator says this.'
    finally:
        if running:
            running.close()


def test_a_revision_change_during_an_outage_is_not_overwritten_by_the_release(world, tmp_path, models, monkeypatch):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    engine = speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=f'http://127.0.0.1:{speech_service.free_port()}',
                           tts_consent=CONSENT)
    world.pipeline.engine = engine
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    clip_id = world.chapter()[0]['segments'][0]['id']
    rev = world.store.one('SELECT audio_rev FROM segment WHERE id = ?', (clip_id,))['audio_rev']
    upload = engine.remote.upload

    def upload_during_outage(*args):
        try:
            upload(*args)
        except speech.Unavailable:
            world.store.invalidate_audio('id = ?', (clip_id,))
            jobs.AUDIO.claim(world.store, clip_id, rev + 1)
            raise
    monkeypatch.setattr(engine.remote, 'upload', upload_during_outage)
    assert world.pipeline.speech_step()
    row = world.store.one('SELECT audio_state, audio_rev FROM segment WHERE id = ?', (clip_id,))
    assert (row['audio_state'], row['audio_rev']) == ('generating', rev + 1)


@pytest.mark.parametrize('case, reason', [
    ('server_error', 'HTTP 500 synthesis_failed: out of memory'),
    ('not_installed', 'HTTP 400 model_not_installed'),
])
def test_definitive_speech_errors_still_fail_the_clip_and_are_not_held_back(world, tmp_path, models, speech_server, case, reason):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    if case == 'server_error':
        speech_server.failure = 'out of memory'
    else:
        speech_server.loader.engines.pop('breeze')
    world.pipeline.engine = remote_engine(tmp_path, speech_server)
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    clip = world.chapter()[0]['segments'][0]
    assert clip['audio_state'] == 'failed' and reason in clip['error']
    assert world.pipeline.retry_at.get('speech') is None and world.pipeline.idle['speech'] is None
    assert not world.pipeline.speech_step()


def test_an_applied_disclaimer_leaves_the_clip_failed_with_no_audio(world, tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    speech_server.disclaimer = 'applied'
    monkeypatch.setattr(speech, 'decode_wav', lambda data: pytest.fail('the audio was decoded'))
    world.pipeline.engine = remote_engine(tmp_path, speech_server)
    world.pipeline.set_focus(world.book['id'], 0, 0, 0, 0)
    assert world.pipeline.speech_step()
    clip = world.chapter()[0]['segments'][0]
    assert clip['audio_state'] == 'failed' and 'HTTP 502 disclaimer_applied' in clip['error']
    assert CONSENT not in clip['error'] and clip['duration'] is None
    assert not world.store.audio_path(world.book['id'], clip['id']).exists()
    assert world.client.get(f"/api/segments/{clip['id']}/audio").status_code == 404


def test_a_spoken_disclaimer_applied_is_refused_and_no_audio_is_decoded(tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.disclaimer = 'applied'
    monkeypatch.setattr(speech, 'decode_wav', lambda data: pytest.fail('the audio was decoded'))
    with pytest.raises(speech.RemoteError, match='HTTP 502 disclaimer_applied') as applied:
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert CONSENT not in str(applied.value)


def test_a_spoken_disclaimer_reported_as_skipped_returns_the_audio(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.disclaimer = 'Skipped'
    audio, rate = remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert len(audio) == rate // 2 and float(audio.mean()) == pytest.approx(0.25, abs=0.0001)


def test_a_missing_disclaimer_header_is_refused_because_the_state_is_unconfirmed(tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.disclaimer = None
    monkeypatch.setattr(speech, 'decode_wav', lambda data: pytest.fail('the audio was decoded'))
    with pytest.raises(RuntimeError, match='could not be confirmed') as unconfirmed:
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert CONSENT not in str(unconfirmed.value)


def test_a_redirect_is_an_error_it_is_not_followed_and_the_voice_is_not_remembered(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    engine = remote_engine(tmp_path, speech_server)
    speech_server.canned['/v1/voices'] = (302, b'', 'text/plain', {'Location': '/login'})
    with pytest.raises(RuntimeError, match='HTTP 302'):
        engine.speak('Hello.', voice, 42)
    assert speech_server.paths == ['/v1/voices'] and engine.uploads == {}
    speech_server.canned.clear()
    engine.speak('Hello.', voice, 42)
    assert speech_server.paths == ['/v1/voices', '/v1/voices', '/v1/audio/speech']


def test_an_upload_answer_other_than_created_is_not_a_saved_voice(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.canned['/v1/voices'] = (200, b'<html>sign in</html>', 'text/html', {})
    engine = remote_engine(tmp_path, speech_server)
    with pytest.raises(RuntimeError, match='did not confirm the voice upload'):
        engine.speak('Hello.', voice, 42)
    assert engine.uploads == {} and speech_server.speeches == []


def test_a_speech_answer_other_than_ok_is_not_audio(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.canned['/v1/audio/speech'] = (202, wav_bytes(), 'audio/wav', {speech.Remote.DISCLAIMER: 'skipped'})
    with pytest.raises(RuntimeError, match='instead of audio'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def test_a_speech_reply_over_the_size_limit_is_refused(tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'voice.wav')
    monkeypatch.setattr(speech.Remote, 'REPLY_LIMIT', 1000)
    with pytest.raises(RuntimeError, match='larger than 1000 bytes'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def gpu_engine(tmp_path, server, provider='chatterbox'):
    return speech.Engine(tmp_path / 'hub', provider, tts_url=server.url, tts_consent=CONSENT)


def asked(**fields):
    return {**fields, 'response_format': 'wav', 'consent_attestation': CONSENT, 'spoken_disclaimer': False}


@pytest.mark.parametrize('provider', list(speech.NARRATORS))
def test_english_voices_are_designed_by_breeze_on_the_service_with_no_model_on_this_mac(tmp_path, models, speech_server, provider):
    audio, rate = gpu_engine(tmp_path, speech_server, provider).design('A low voice.', 42)
    assert speech_server.speeches == [asked(model='breeze', input=speech.SAMPLE_TEXT['en'][1], instructions='A low voice.',
                                            cfg_scale=4.0, seed=42)]
    assert speech_server.paths == ['/v1/audio/speech'] and speech_server.uploads == []
    assert rate == 24000 and len(audio) == speech.SAMPLE_SECONDS * rate
    assert models.loads == models.snapshots == models.transcribed == []


@pytest.mark.parametrize('language, said', [('es', 'es'), ('fr', 'en')])
def test_other_languages_are_designed_by_qwen_on_the_service_in_the_language_of_the_sample(tmp_path, models, speech_server,
                                                                                           language, said):
    gpu_engine(tmp_path, speech_server).design('Una voz grave.', 7, language=language)
    assert speech_server.speeches == [asked(model='qwen', input=speech.SAMPLE_TEXT[said][1], instructions='Una voz grave.',
                                            language=said, seed=7)]
    assert models.loads == []


def test_a_male_voice_above_its_ceiling_is_designed_again_on_the_service_with_the_next_seed(tmp_path, models, speech_server,
                                                                                            monkeypatch):
    pitches = iter([300., 240., 190.])
    monkeypatch.setattr(speech, 'median_pitch', lambda audio, rate: next(pitches))
    gpu_engine(tmp_path, speech_server).design('A young man.', 42, ceiling=210)
    assert [call['seed'] for call in speech_server.speeches] == [42, 43, 44]


@pytest.mark.parametrize('state', ['applied', 'unknown'])
def test_a_design_whose_reply_reports_a_disclaimer_other_than_skipped_is_refused(tmp_path, models, speech_server, state):
    speech_server.design_disclaimer = state
    with pytest.raises(speech.RemoteError, match='disclaimer'):
        gpu_engine(tmp_path, speech_server).design('A low voice.', 42)


def test_a_take_is_directed_by_breeze_on_the_service_from_the_uploaded_voice_and_its_mac_transcript(tmp_path, models,
                                                                                                    speech_server):
    voice = recording(tmp_path / 'voice.wav', rate=44100)
    models.transcript = 'The saved words.'
    engine = gpu_engine(tmp_path, speech_server)
    audio, rate = engine.direct(voice, 'angry', 9)
    instruction, script = DIRECTIONS['angry']
    wav = voice.read_bytes()
    assert speech_server.uploads == [{'voice': wav, 'name': name_for(wav), 'consent_attestation': CONSENT}]
    assert speech_server.speeches == [asked(model='breeze', input=script, voice=name_for(wav), ref_text='The saved words.',
                                            instructions=instruction, cfg_scale=1.0, seed=9)]
    assert rate == 24000 and len(audio) == speech.SAMPLE_SECONDS * rate
    assert models.loads == [speech.TRANSCRIBER]
    engine.direct(voice, 'sad', 9)
    assert len(speech_server.uploads) == 1 and len(models.transcribed) == 1


def test_a_sample_that_sounds_like_a_voice_in_use_is_shifted_by_the_service_speaker_comparison(tmp_path, models, speech_server):
    other = recording(tmp_path / 'other.wav', level=0.3)
    # Only the unshifted 15 second sample passes for the other voice; any shift tells them apart.
    speech_server.embed = lambda audio, rate: [1., 0.] if len(audio) in (7 * rate, 15 * rate) else [0., 1.]
    engine = gpu_engine(tmp_path, speech_server)
    audio, rate = engine.design('A low voice.', 42, language='es', others=[other])
    assert len(audio) > speech.SAMPLE_SECONDS * rate, 'shifted down, so played slower'
    assert [heard['language'] for heard in speech_server.heard] == ['es'] * 3
    assert speech_server.heard[0] == {'seconds': 7, 'level': pytest.approx(0.3, abs=0.001), 'language': 'es'}
    engine.design('A low voice.', 43, language='es', others=[other])
    assert len(speech_server.heard) == 5, 'the voice in use is heard once'
    assert models.loads == []


@pytest.mark.parametrize('reply', [
    (200, b'{"embedding": []}', 'application/json', {}),
    (200, b'{"embedding": [0, 0]}', 'application/json', {}),
    (200, b'{"embedding": [[1, 0]]}', 'application/json', {}),
    (200, b'{"embedding": ["a"]}', 'application/json', {}),
    (200, b'{"vector": [1, 0]}', 'application/json', {}),
    (200, b'not json', 'text/plain', {}),
    (202, b'{"embedding": [1, 0]}', 'application/json', {}),
])
def test_a_speaker_answer_that_is_not_a_usable_embedding_is_an_error(tmp_path, models, speech_server, reply):
    speech_server.canned['/v1/speaker'] = reply
    with pytest.raises(speech.RemoteError, match='speaker embedding') as failed:
        gpu_engine(tmp_path, speech_server).design('A low voice.', 42, others=[recording(tmp_path / 'other.wav')])
    assert not isinstance(failed.value, speech.Unavailable)


def test_a_speaker_comparison_while_the_service_loads_is_unavailable(tmp_path, models, speech_server):
    speech_server.hold('chatterbox-turbo')
    with pytest.raises(speech.Unavailable, match='HTTP 503 loading'):
        gpu_engine(tmp_path, speech_server).design('A low voice.', 42, others=[recording(tmp_path / 'other.wav')])
    assert speech_server.paths == ['/v1/audio/speech', '/v1/speaker']


def test_the_speaker_embedding_comes_back_as_a_unit_vector(speech_server):
    speech_server.embed = lambda audio, rate: [3., 4.]
    heard = speech.Remote(speech_server.url, CONSENT).speaker(wav_bytes(), 'en')
    np.testing.assert_allclose(heard, [0.6, 0.8])


@pytest.mark.parametrize('language', ['en', 'es'])
def test_chatterbox_narrates_on_the_service_and_a_rambling_line_is_retaken_with_the_next_seed(tmp_path, models, speech_server,
                                                                                             language):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.replies = [wav_bytes(seconds=9), wav_bytes(seconds=0.5)]
    audio, rate = gpu_engine(tmp_path, speech_server).speak('Hello.', voice, 42, language=language)
    name = name_for(voice.read_bytes())
    assert speech_server.speeches == [asked(model='chatterbox', input='Hello.', voice=name, language=language, seed=seed)
                                      for seed in (42, 43)]
    assert len(speech_server.uploads) == 1 and len(audio) == rate // 2
    assert models.loads == [], 'no speech model on this Mac, not even Whisper'


def test_a_line_that_keeps_rambling_is_kept_after_three_tries(tmp_path, models, speech_server):
    speech_server.replies = [wav_bytes(seconds=9)] * 4
    audio, rate = gpu_engine(tmp_path, speech_server).speak('Hello.', recording(tmp_path / 'voice.wav'), 42)
    assert [call['seed'] for call in speech_server.speeches] == [42, 43, 44] and len(audio) == 9 * rate


@pytest.mark.parametrize('provider, model', [('fish', speech.FISH), ('breeze', speech.BREEZE), ('qwen-base', speech.QWEN_BASE)])
def test_local_narrators_still_narrate_on_this_mac_when_the_service_designs(tmp_path, models, speech_server, provider, model):
    voice = recording(tmp_path / 'voice.wav')
    engine = gpu_engine(tmp_path, speech_server, provider)
    engine.design('A low voice.', 42)
    engine.speak('Hello.', voice, 42)
    assert [call['model'] for call in speech_server.speeches] == ['breeze']
    assert models.loads == [speech.TRANSCRIBER, model]


def test_with_the_service_chatterbox_designs_directs_compares_and_narrates_without_an_mlx_speech_model(tmp_path, models,
                                                                                                     speech_server):
    engine = gpu_engine(tmp_path, speech_server)
    first, second = tmp_path / 'first.wav', tmp_path / 'second.wav'
    sf.write(first, *engine.design('A low voice.', 1), format='WAV')
    sf.write(second, *engine.design('A high voice.', 2, others=[first]), format='WAV')
    take = tmp_path / 'take.wav'
    speech_server.replies = [wav_bytes(seconds=20, level=0.3)]
    sf.write(take, *engine.direct(second, 'happy', 3), format='WAV')
    engine.speak('Hello.', take, 4)
    assert [call['model'] for call in speech_server.speeches] == ['breeze', 'breeze', 'breeze', 'chatterbox']
    assert models.loads == [speech.TRANSCRIBER], 'Whisper once, for the take'
    assert {repo for repo, _ in models.snapshots} == {speech.TRANSCRIBER}


PINNED_IDENTITY = {
    'ming': {'provider': 'ming', 'policy': 1, 'transcription_policy': 'whisper-auto-language-16khz-v1',
             'reference_silence': 1.0, 'models': [
        {'id': 'mlx-community/Ming-3.0-tts-0.5B-8bit', 'revision': 'd0d1da6f627e4b3f20cc2db6ba4aee86d45343ad'},
        {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'},
        {'id': 'mlx-community/whisper-large-v3-turbo-asr-fp16', 'revision': '624c19c9af5603fa73b83bce14d4aeea96156d18'}]},
    'chatterbox': {'provider': 'chatterbox', 'policy': 1, 'models': [
        {'id': 'mlx-community/chatterbox-turbo-8bit'}, {'id': 'mlx-community/chatterbox-8bit'},
        {'id': 'mlx-community/S3TokenizerV2'}, {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'}]},
    'fish': {'provider': 'fish', 'policy': 1, 'transcription_policy': 'whisper-auto-language-16khz-v1', 'models': [
        {'id': 'mlx-community/fish-audio-s2-pro-8bit'}, {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'},
        {'id': 'mlx-community/whisper-large-v3-turbo-asr-fp16', 'revision': '624c19c9af5603fa73b83bce14d4aeea96156d18'}]},
    'breeze': {'provider': 'breeze', 'policy': 1, 'transcription_policy': 'whisper-auto-language-16khz-v1', 'models': [
        {'id': 'mlx-community/Breeze-TTS-2-mlx-4bit'}, {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'},
        {'id': 'mlx-community/whisper-large-v3-turbo-asr-fp16', 'revision': '624c19c9af5603fa73b83bce14d4aeea96156d18'}]},
    'breeze-gguf': {'provider': 'breeze-gguf', 'policy': 1, 'transcription_policy': 'whisper-auto-language-16khz-v1',
                    'spoken_disclaimer': False, 'models': [
        {'id': 'cstr/breeze-tts-2-GGUF@q4_k'}, {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'},
        {'id': 'mlx-community/whisper-large-v3-turbo-asr-fp16', 'revision': '624c19c9af5603fa73b83bce14d4aeea96156d18'}]},
    'qwen-base': {'provider': 'qwen-base', 'policy': 1, 'transcription_policy': 'whisper-auto-language-16khz-v1',
                  'reference_silence': 1.0, 'soft_end': True, 'models': [
        {'id': 'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit', 'revision': '50f45ef0047cde7e84c2ef04326acb8ada2436a7'},
        {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'},
        {'id': 'mlx-community/whisper-large-v3-turbo-asr-fp16', 'revision': '624c19c9af5603fa73b83bce14d4aeea96156d18'}]},
    'qwen-base-gpu': {'provider': 'qwen-base-gpu', 'policy': 1, 'transcription_policy': 'whisper-auto-language-16khz-v1',
                      'spoken_disclaimer': False, 'reference_silence': 1.0, 'soft_end': True,
                      'models': [
        {'id': 'Qwen/Qwen3-TTS-12Hz-0.6B-Base', 'revision': '5d83992436eae1d760afd27aff78a71d676296fc'},
        {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'},
        {'id': 'mlx-community/whisper-large-v3-turbo-asr-fp16', 'revision': '624c19c9af5603fa73b83bce14d4aeea96156d18'}]},
    'audio8': {'provider': 'audio8', 'policy': 1, 'transcription_policy': 'whisper-auto-language-16khz-v1',
               'spoken_disclaimer': False, 'soft_end': True, 'models': [
        {'id': 'Edge0/Audio8-TTS-Realistic-Air', 'revision': '23c305cc013fde378edb459f538317043c993a74'},
        {'id': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'},
        {'id': 'mlx-community/whisper-large-v3-turbo-asr-fp16', 'revision': '624c19c9af5603fa73b83bce14d4aeea96156d18'}]},
}


@pytest.mark.parametrize('provider', list(speech.NARRATORS))
def test_generation_identity_is_pinned_and_does_not_depend_on_where_the_models_run(tmp_path, provider):
    with_service = speech.Engine(tmp_path / 'hub', provider, tts_url='http://127.0.0.1:9', tts_consent=CONSENT)
    assert with_service.generation_identity == PINNED_IDENTITY[provider]
    if provider not in ('breeze-gguf', 'audio8', 'qwen-base-gpu'):
        assert speech.Engine(tmp_path / 'hub', provider).generation_identity == PINNED_IDENTITY[provider]


def test_moving_chatterbox_to_the_service_keeps_every_ready_clip(world, tmp_path):
    from meread.app import create_app

    world.pipeline.set_focus(world.book['id'], 0, 1, 0, 1)
    world.drain()
    before = world.store.all('SELECT id, audio_state, audio_rev, duration FROM segment')
    assert [clip for clip in before if clip['audio_state'] == 'ready']
    engine = speech.Engine(tmp_path / 'hub', 'chatterbox', tts_url='http://127.0.0.1:9', tts_consent=CONSENT)
    moved = create_app(tmp_path, engine=engine, run_workers=False)
    assert moved.state.store.all('SELECT id, audio_state, audio_rev, duration FROM segment') == before


class SpeechServerAddress:
    url = 'http://127.0.0.1:9'


def default_engine(tmp_path, server):
    return speech.Engine(tmp_path / 'hub', tts_url=server.url, tts_consent=CONSENT)


def gpu_engine_at(tmp_path, provider):
    return gpu_engine(tmp_path, SpeechServerAddress, provider)


def test_by_default_the_service_narrates_english_with_chatterbox_and_spanish_with_audio8(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    models.transcript = 'The saved words.'
    engine = default_engine(tmp_path, speech_server)
    engine.speak('Hello.', voice, 42, language='en')
    engine.speak('Hola.', voice, 43, language='es')
    name = name_for(voice.read_bytes())
    assert speech_server.speeches == [
        asked(model='chatterbox', input='Hello.', voice=name, language='en', seed=42),
        asked(model='audio8', input='Hola.', voice=name, language='es', ref_text='The saved words.', seed=43)]
    assert len(speech_server.uploads) == 1, 'one upload serves both narrators'
    assert models.loads == [speech.TRANSCRIBER], 'Whisper only for the Audio8 line'


def test_audio8_can_narrate_english_too_when_chosen(tmp_path, models, speech_server):
    models.transcript = 'The saved words.'
    engine = gpu_engine(tmp_path, speech_server, 'audio8')
    engine.speak('Hello.', recording(tmp_path / 'voice.wav'), 42, language='en')
    assert [(call['model'], call['language'], call['ref_text']) for call in speech_server.speeches] == [
        ('audio8', 'en', 'The saved words.')]


@pytest.mark.parametrize('seconds', [11.8, 15])
def test_qwen_base_gpu_narrates_on_the_service_with_the_transcript_and_language(tmp_path, models, speech_server, seconds):
    models.transcript = 'The saved words.'
    engine = gpu_engine(tmp_path, speech_server, 'qwen-base-gpu')
    voice = speech_with_pauses(tmp_path / 'voice.wav', [
        (4, True), (0.6, False), (4, True), (0.4, False), (seconds - 9, True),
    ])
    engine.speak('Hola.', voice, 42, language='es')
    padded = speech.with_silence(voice.read_bytes(), 1.0)
    assert speech_server.speeches == [asked(model='qwen-base', input='Hola.', voice=name_for(padded), language='es',
                                            ref_text='The saved words.', seed=42)]
    assert speech_server.voices == {name_for(padded): padded}, 'the service gets the voice followed by a second of silence'
    assert models.loads == [speech.TRANSCRIBER], 'Whisper here, the model on the service'

    assert len(models.transcribed[0]) / 16000 == pytest.approx(seconds, abs=0.01)


def test_a_rambling_qwen_base_gpu_line_is_retaken_with_the_next_seed(tmp_path, models, speech_server):
    speech_server.replies = [wav_bytes(seconds=9), wav_bytes(seconds=0.5)]
    audio, rate = gpu_engine(tmp_path, speech_server, 'qwen-base-gpu').speak('Hola.', recording(tmp_path / 'voice.wav'), 42,
                                                                             language='es')
    assert [call['seed'] for call in speech_server.speeches] == [42, 43] and len(audio) == rate // 2 + int(0.2 * rate)


def test_qwen_base_gpu_needs_a_speech_service(tmp_path):
    with pytest.raises(ValueError, match='--tts-url is required.*qwen-base-gpu'):
        speech.Engine(tmp_path / 'hub', 'qwen-base-gpu')


def test_a_rambling_audio8_line_is_retaken_with_the_next_seed(tmp_path, models, speech_server):
    speech_server.replies = [wav_bytes(seconds=9), wav_bytes(seconds=0.5)]
    audio, rate = gpu_engine(tmp_path, speech_server, 'audio8').speak('Hola.', recording(tmp_path / 'voice.wav'), 42, language='es')
    assert [call['seed'] for call in speech_server.speeches] == [42, 43] and len(audio) == rate // 2 + int(0.2 * rate)


def test_without_a_service_spanish_stays_with_chatterbox_on_this_mac(tmp_path):
    engine = speech.Engine(tmp_path / 'hub')
    assert [engine.identity(language)['provider'] for language in ('en', 'es')] == ['chatterbox', 'chatterbox']
    assert engine.generation_identity == PINNED_IDENTITY['chatterbox']


def test_with_a_service_the_default_identity_names_each_languages_narrator(tmp_path):
    identity = default_engine(tmp_path, SpeechServerAddress).generation_identity
    assert identity == {'narrators': {'en': PINNED_IDENTITY['chatterbox'], 'es': PINNED_IDENTITY['audio8']}}


def test_choosing_a_narrator_overrides_the_language_defaults(tmp_path):
    for choice in ('chatterbox', 'audio8'):
        engine = gpu_engine_at(tmp_path, choice)
        assert {engine.identity(language)['provider'] for language in ('en', 'es')} == {choice}
        assert engine.generation_identity == PINNED_IDENTITY[choice]


def test_switching_the_spanish_default_regenerates_only_spanish_clips(world, tmp_path):
    from meread.app import create_app
    from conftest import SPANISH, make_epub

    spanish = world.client.post('/api/books', files={'file': ('es.epub', make_epub((SPANISH,)), 'application/epub+zip')}).json()
    for book in (world.book, spanish):
        world.client.post(f"/api/books/{book['id']}/focus", json={'first': 0, 'last': 0, 'current_first': 0, 'current_last': 0})
        world.pipeline.set_focus(book['id'], 0, 0, 0, 0)
        world.drain()
    english_before = world.store.all("SELECT id, audio_state, audio_rev FROM segment WHERE book_id = ?", (world.book['id'],))
    spanish_before = world.store.all("SELECT id, audio_state, audio_rev FROM segment WHERE book_id = ?", (spanish['id'],))
    assert any(clip['audio_state'] == 'ready' for clip in english_before) and any(clip['audio_state'] == 'ready' for clip in spanish_before)

    engine = default_engine(tmp_path, SpeechServerAddress)
    reopened = create_app(tmp_path, engine=engine, run_workers=False)
    assert reopened.state.store.all("SELECT id, audio_state, audio_rev FROM segment WHERE book_id = ?", (world.book['id'],)) == english_before
    after = reopened.state.store.all("SELECT id, audio_state, audio_rev FROM segment WHERE book_id = ?", (spanish['id'],))
    assert after and all(clip['audio_state'] == 'none' and clip['audio_rev'] > before['audio_rev']
                         for clip, before in zip(after, spanish_before))

    again = create_app(tmp_path, engine=default_engine(tmp_path, SpeechServerAddress), run_workers=False)
    assert again.state.store.all("SELECT id, audio_state, audio_rev FROM segment WHERE book_id = ?", (spanish['id'],)) == after
