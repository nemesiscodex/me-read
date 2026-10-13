import email.parser
import email.policy
import hashlib
import http.server
import io
import json
import socket
import threading
import time
import types

import numpy as np
import pytest
import soundfile as sf

from meread import pipeline as pipeline_module
from meread import speech
from test_speech import models, recording  # noqa: F401  the local stubs the remote engine must never reach

CONSENT = 'I own these reference voices and allow them to be cloned for my own books.'


def name_for(wav):
    return 'mr-' + hashlib.sha256(wav).hexdigest()[:32]


def wav_bytes(rate=24000, seconds=0.5, level=0.25, channels=1, subtype='PCM_16'):
    buffer = io.BytesIO()
    sf.write(buffer, np.full((int(rate * seconds), channels), level, dtype='float32'), rate, format='WAV', subtype=subtype)
    return buffer.getvalue()


def failure(status, code, message):
    return status, json.dumps({'error': {'code': code, 'message': message}}).encode(), 'application/json', {}


def fields_of(content_type, body):
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    fields = {}
    for part in message.iter_parts():
        payload = part.get_payload(decode=True)
        fields[part.get_param('name', header='content-disposition')] = payload if part.get_filename() else payload.decode()
    return fields


class SpeechServer:
    """Emulates the speech server's contract as tested: named voices, consent, a transcript per voice, WAV replies."""

    def __init__(self):
        self.url = None
        self.consent = CONSENT
        self.voices, self.transcripts = {}, {}
        self.paths, self.uploads, self.speeches = [], [], []
        self.upload_failures, self.speech_failures = [], []
        self.sample_rate, self.reply, self.broken = 24000, None, False
        self.speech_delay = 0
        self.disclaimer = 'skipped'
        self.redirect, self.upload_reply, self.speech_status = False, None, 200

    def answer(self, path, content_type, body):
        self.paths.append(path)
        if path == '/login':
            return 200, b'<html>sign in</html>', 'text/html', {}
        if self.redirect:
            return 302, b'', 'text/plain', {'Location': '/login'}
        if path == '/v1/voices':
            return self.upload(fields_of(content_type, body))
        if path == '/v1/audio/speech':
            return self.synthesize(json.loads(body))
        return failure(404, 'not_found', path)

    def upload(self, fields):
        self.uploads.append(fields)
        if self.upload_reply:
            return self.upload_reply
        if self.upload_failures:
            return failure(*self.upload_failures.pop(0))
        if fields.get('consent_attestation') != self.consent:
            return failure(400, 'consent_required', 'consent_attestation is required')
        name = fields['name']
        if name in self.voices:
            return failure(409, 'voice_exists', 'a voice with that name already exists')
        self.voices[name], self.transcripts[name] = fields['voice'], fields.get('transcript', '')
        return 201, json.dumps({'format': 'wav', 'name': name}).encode(), 'application/json', {}

    def synthesize(self, body):
        self.speeches.append(body)
        if self.speech_delay:
            time.sleep(self.speech_delay)
        if self.speech_failures:
            return failure(*self.speech_failures.pop(0))
        if body.get('consent_attestation') != self.consent:
            return failure(400, 'consent_required', 'consent_attestation is required')
        voice = body.get('voice')
        if self.broken or voice not in self.voices or not self.transcripts.get(voice):
            return failure(500, 'synthesis_failed', 'the voice is unknown or has no transcript')
        reply = self.reply if self.reply is not None else wav_bytes(self.sample_rate)
        headers = {} if self.disclaimer is None else {'X-Crispasr-Spoken-Disclaimer': self.disclaimer}
        return self.speech_status, reply, 'audio/wav', headers


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.answer(b'')

    def do_POST(self):
        self.answer(self.rfile.read(int(self.headers['Content-Length'])))

    def answer(self, body):
        server = self.server.speech_server
        target = self.raw_requestline.split()[1].decode()
        status, reply, content_type, headers = server.answer(target, self.headers.get('Content-Type', ''), body)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def serve(fake, port=0):
    httpd = http.server.HTTPServer(('127.0.0.1', port), Handler)
    httpd.speech_server = fake
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def speech_server():
    fake = SpeechServer()
    httpd = serve(fake)
    fake.url = f'http://127.0.0.1:{httpd.server_port}'
    yield fake
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail('a network call was made')
    monkeypatch.setattr(speech.urllib.request, 'urlopen', refused)


def remote_engine(tmp_path, server, consent=CONSENT):
    return speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=server.url, tts_consent=consent)


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
        'input': 'Hello there.', 'voice': name_for(voice.read_bytes()), 'ref_text': 'The saved words.', 'seed': 77,
        'response_format': 'wav', 'consent_attestation': CONSENT, 'spoken_disclaimer': False}]
    upload = speech_server.uploads[0]
    assert (upload['transcript'], upload['consent_attestation'], upload['name']) == (
        'The saved words.', CONSENT, name_for(voice.read_bytes()))
    assert float(models.transcribed[0].mean()) == pytest.approx(0.1, abs=0.0001)


@pytest.mark.parametrize('rate', [24000, 44100])
def test_returned_wav_becomes_mono_float32_at_the_rate_the_server_reports(tmp_path, models, speech_server, rate):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.sample_rate = rate
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


def test_consent_refused_by_the_server_stops_before_any_speech_request(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    with pytest.raises(RuntimeError, match=r'HTTP 400 consent_required') as refused:
        remote_engine(tmp_path, speech_server, consent='someone else said yes').speak('Hello.', voice, 42)
    assert 'someone else said yes' not in str(refused.value)
    assert speech_server.speeches == [] and speech_server.voices == {}


def test_server_error_on_speech_is_reported_with_its_code_and_nothing_else(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.speech_failures = [(500, 'synthesis_failed', 'out of memory')]
    with pytest.raises(RuntimeError, match=r'HTTP 500 synthesis_failed: out of memory') as failed:
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert CONSENT not in str(failed.value) and 'Hello' not in str(failed.value)


def test_server_still_loading_is_reported_as_http_503(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.speech_failures = [(503, 'loading', 'model is loading')]
    with pytest.raises(speech.Unavailable, match='HTTP 503 loading'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def test_upload_failure_sends_no_speech_request(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.upload_failures = [(503, 'loading', 'model is loading')]
    with pytest.raises(speech.Unavailable, match='HTTP 503 loading'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
    assert speech_server.speeches == []


def test_an_existing_name_on_upload_means_the_voice_is_already_there(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    remote_engine(tmp_path, speech_server).speak('First.', voice, 42)
    audio, rate = remote_engine(tmp_path, speech_server).speak('Second.', voice, 42)
    assert len(speech_server.uploads) == 2 and len(audio) == rate // 2
    assert [call['voice'] for call in speech_server.speeches] == [name_for(voice.read_bytes())] * 2


def test_unreachable_server_is_reported_without_the_consent_text(tmp_path, models):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    voice = recording(tmp_path / 'voice.wav')
    engine = speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=f'http://127.0.0.1:{port}', tts_consent=CONSENT)
    with pytest.raises(speech.Unavailable, match='Could not reach the speech server at 127.0.0.1') as unreachable:
        engine.speak('Hello.', voice, 42)
    assert CONSENT not in str(unreachable.value)


def test_slow_speech_request_times_out_with_a_clear_error(tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'voice.wav')
    monkeypatch.setattr(speech.Remote, 'SPEECH_TIMEOUT', 0.1)
    speech_server.speech_delay = 0.5
    with pytest.raises(speech.Unavailable, match='did not answer within 0.1 seconds'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


@pytest.mark.parametrize('reply, message', [
    (b'<html>not audio</html>', 'not a readable WAV'),
    (wav_bytes(seconds=0), 'no audio'),
    (wav_bytes(level=float('nan'), subtype='FLOAT'), 'invalid mono'),
    (wav_bytes(channels=2), 'invalid mono'),
])
def test_unusable_wav_from_the_server_never_becomes_audio(tmp_path, models, speech_server, reply, message):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.reply = reply
    with pytest.raises(RuntimeError, match=message):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def test_forgotten_voice_is_uploaded_again_once_and_the_request_retried_once(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    engine = remote_engine(tmp_path, speech_server)
    engine.speak('One.', voice, 42)
    speech_server.voices.clear()
    audio, rate = engine.speak('Two.', voice, 42)
    assert len(speech_server.uploads) == 2 and len(speech_server.speeches) == 3
    assert speech_server.voices == {name_for(voice.read_bytes()): voice.read_bytes()} and len(audio) == rate // 2


def test_a_server_that_keeps_failing_gets_exactly_one_retry_not_a_loop(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    engine = remote_engine(tmp_path, speech_server)
    engine.speak('One.', voice, 42)
    speech_server.voices.clear()
    speech_server.broken = True
    uploads, speeches = len(speech_server.uploads), len(speech_server.speeches)
    with pytest.raises(RuntimeError, match='synthesis_failed'):
        engine.speak('Two.', voice, 42)
    assert len(speech_server.uploads) - uploads == 1
    assert len(speech_server.speeches) - speeches == 2


def test_a_fresh_upload_that_is_followed_by_a_failed_speech_is_not_retried(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.broken = True
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
    speech_server.speech_failures = [(500, 'synthesis_failed', 'out of memory')]
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


def refused_port():
    httpd = serve(SpeechServer())
    port = httpd.server_port
    httpd.shutdown()
    httpd.server_close()
    return port


def test_a_speech_server_outage_waits_then_retries_and_no_clip_fails(world, tmp_path, models, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pipeline_module, 'time', types.SimpleNamespace(monotonic=lambda: clock[0]))
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    models.transcript = 'The saved narrator says this.'
    fake, port, running, states = SpeechServer(), refused_port(), None, []
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
        fake.upload_failures = [(503, 'loading', 'model is loading')]
        running = serve(fake, port)
        clock[0] = 1015
        assert world.pipeline.speech_step()
        assert clip()['audio_state'] == 'none' and fake.paths == ['/v1/voices']
        assert world.pipeline.retry_at['speech'] == 1030
        clock[0] = 1029
        assert not world.pipeline.speech_step()
        assert fake.paths == ['/v1/voices']
        clock[0] = 1030
        assert world.pipeline.speech_step()
        ready = clip()
        assert (ready['audio_state'], ready['duration']) == ('ready', 0.5)
        assert world.pipeline.idle['speech'] is None
        assert 'failed' not in states
        assert fake.paths == ['/v1/voices', '/v1/voices', '/v1/audio/speech']
        assert fake.speeches[0]['ref_text'] == 'The saved narrator says this.'
    finally:
        if running:
            running.shutdown()
            running.server_close()


def test_a_revision_change_during_an_outage_is_not_overwritten_by_the_release(world, tmp_path, models, monkeypatch):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    engine = speech.Engine(tmp_path / 'hub', 'breeze-gguf', tts_url=f'http://127.0.0.1:{refused_port()}', tts_consent=CONSENT)
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
            world.store.run("UPDATE segment SET audio_state = 'generating' WHERE id = ? AND audio_rev = ?", (clip_id, rev + 1))
            raise
    monkeypatch.setattr(engine.remote, 'upload', upload_during_outage)
    assert world.pipeline.speech_step()
    row = world.store.one('SELECT audio_state, audio_rev FROM segment WHERE id = ?', (clip_id,))
    assert (row['audio_state'], row['audio_rev']) == ('generating', rev + 1)


@pytest.mark.parametrize('case, reason', [
    ('server_error', 'HTTP 500 synthesis_failed: out of memory'),
    ('consent', 'HTTP 400 consent_required'),
])
def test_definitive_speech_errors_still_fail_the_clip_and_are_not_held_back(world, tmp_path, models, speech_server, case, reason):
    voice = recording(tmp_path / 'upload.wav')
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/voice",
                             files={'file': ('voice.wav', voice.read_bytes(), 'audio/wav')}).status_code == 200
    if case == 'server_error':
        speech_server.speech_failures = [(500, 'synthesis_failed', 'out of memory')]
        world.pipeline.engine = remote_engine(tmp_path, speech_server)
    else:
        world.pipeline.engine = remote_engine(tmp_path, speech_server, consent='someone else said yes')
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
    assert clip['audio_state'] == 'failed' and '--accept-marking-responsibility' in clip['error']
    assert CONSENT not in clip['error'] and clip['duration'] is None
    assert not world.store.audio_path(world.book['id'], clip['id']).exists()
    assert world.client.get(f"/api/segments/{clip['id']}/audio").status_code == 404


def test_a_spoken_disclaimer_applied_is_refused_and_no_audio_is_decoded(tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.disclaimer = 'applied'
    monkeypatch.setattr(speech, 'decode_wav', lambda data: pytest.fail('the audio was decoded'))
    with pytest.raises(RuntimeError, match='--accept-marking-responsibility') as applied:
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
    speech_server.redirect = True
    with pytest.raises(RuntimeError, match='HTTP 302') as redirected:
        engine.speak('Hello.', voice, 42)
    assert speech_server.paths == ['/v1/voices'] and speech_server.speeches == []
    assert engine.uploads == {} and 'sign in' not in str(redirected.value)
    speech_server.redirect = False
    engine.speak('Hello.', voice, 42)
    assert len(speech_server.uploads) == 1 and len(speech_server.speeches) == 1


def test_an_upload_answer_other_than_created_is_not_a_saved_voice(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.upload_reply = (200, b'<html>sign in</html>', 'text/html', {})
    engine = remote_engine(tmp_path, speech_server)
    with pytest.raises(RuntimeError, match='did not confirm the voice upload'):
        engine.speak('Hello.', voice, 42)
    assert engine.uploads == {} and speech_server.speeches == []


def test_a_speech_answer_other_than_ok_is_not_audio(tmp_path, models, speech_server):
    voice = recording(tmp_path / 'voice.wav')
    speech_server.speech_status = 202
    with pytest.raises(RuntimeError, match='instead of audio'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)


def test_a_speech_reply_over_the_size_limit_is_refused(tmp_path, models, speech_server, monkeypatch):
    voice = recording(tmp_path / 'voice.wav')
    monkeypatch.setattr(speech.Remote, 'REPLY_LIMIT', 1000)
    with pytest.raises(RuntimeError, match='larger than 1000 bytes'):
        remote_engine(tmp_path, speech_server).speak('Hello.', voice, 42)
