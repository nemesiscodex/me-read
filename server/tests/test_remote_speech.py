import base64
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
from meread.emotions import DIRECTIONS
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
    """Emulates the speech server's contract as tested: named voices, consent, a transcript per Breeze request, WAV replies."""

    def __init__(self):
        self.url = None
        self.consent = CONSENT
        self.voices = {}
        self.paths, self.uploads, self.speeches = [], [], []
        self.upload_failures, self.speech_failures = [], []
        self.sample_rate, self.reply, self.broken = 24000, None, False
        self.speech_delay = 0
        self.disclaimer = 'skipped'
        self.redirect, self.upload_reply, self.speech_status = False, None, 200
        self.replies, self.design_disclaimer = [], None
        self.heard, self.embed, self.speaker_reply = [], lambda audio, rate: [1., 0.], None

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
        if path == '/v1/speaker':
            return self.speaker(json.loads(body))
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
        self.voices[name] = fields['voice']
        return 201, json.dumps({'format': 'wav', 'name': name}).encode(), 'application/json', {}

    def synthesize(self, body):
        self.speeches.append(body)
        if self.speech_delay:
            time.sleep(self.speech_delay)
        if self.speech_failures:
            return failure(*self.speech_failures.pop(0))
        voice, model = body.get('voice'), body.get('model')
        if voice and body.get('consent_attestation') != self.consent:
            return failure(400, 'consent_required', 'consent_attestation is required')
        if (model not in ('breeze', 'qwen', 'chatterbox') or (model == 'chatterbox' and not voice)
                or (model == 'qwen' and (voice or not body.get('instructions')))):
            return failure(400, 'invalid_request', f'{model} cannot take these fields')
        if voice is None:
            headers = {} if self.design_disclaimer is None else {'X-Crispasr-Spoken-Disclaimer': self.design_disclaimer}
            return 200, wav_bytes(self.sample_rate, seconds=20), 'audio/wav', headers
        if model == 'breeze' and not body.get('ref_text'):
            return failure(400, 'ref_text_required', 'a Breeze clone needs the transcript of its voice')
        if self.broken or voice not in self.voices:
            return failure(500, 'synthesis_failed', 'the voice is unknown')
        # A take is a 15 second direction of the voice; a line is short.
        default = wav_bytes(self.sample_rate, seconds=20 if body.get('instructions') else 0.5)
        reply = self.replies.pop(0) if self.replies else self.reply if self.reply is not None else default
        headers = {} if self.disclaimer is None else {'X-Crispasr-Spoken-Disclaimer': self.disclaimer}
        return self.speech_status, reply, 'audio/wav', headers

    def speaker(self, body):
        audio, rate = sf.read(io.BytesIO(base64.b64decode(body['audio'])), dtype='float32')
        self.heard.append({'seconds': len(audio) / rate, 'level': float(audio.mean()), 'language': body['language']})
        if self.speaker_reply:
            return self.speaker_reply
        return 200, json.dumps({'embedding': self.embed(audio, rate)}).encode(), 'application/json', {}


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
        'model': 'breeze', 'input': 'Hello there.', 'voice': name_for(voice.read_bytes()), 'ref_text': 'The saved words.', 'seed': 77,
        'response_format': 'wav', 'consent_attestation': CONSENT, 'spoken_disclaimer': False}]
    assert speech_server.uploads == [{'voice': voice.read_bytes(), 'name': name_for(voice.read_bytes()),
                                      'consent_attestation': CONSENT}]
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


def gpu_engine(tmp_path, server, provider='chatterbox'):
    return speech.Engine(tmp_path / 'hub', provider, tts_url=server.url, tts_consent=CONSENT)


def asked(**fields):
    return {**fields, 'response_format': 'wav', 'consent_attestation': CONSENT, 'spoken_disclaimer': False}


@pytest.mark.parametrize('provider', list(speech.PROVIDERS))
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
                                            instructions=instruction, cfg_scale=4.0, seed=9)]
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
    speech_server.speaker_reply = reply
    with pytest.raises(speech.RemoteError, match='speaker embedding') as failed:
        gpu_engine(tmp_path, speech_server).design('A low voice.', 42, others=[recording(tmp_path / 'other.wav')])
    assert not isinstance(failed.value, speech.Unavailable)


def test_a_speaker_comparison_while_the_service_loads_is_unavailable(tmp_path, models, speech_server):
    speech_server.speaker_reply = failure(503, 'loading', 'chatterbox is loading')
    with pytest.raises(speech.Unavailable, match='HTTP 503 loading'):
        gpu_engine(tmp_path, speech_server).design('A low voice.', 42, others=[recording(tmp_path / 'other.wav')])


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


@pytest.mark.parametrize('provider', ['fish', 'breeze'])
def test_fish_and_breeze_still_narrate_on_this_mac_when_the_service_designs(tmp_path, models, speech_server, provider):
    voice = recording(tmp_path / 'voice.wav')
    engine = gpu_engine(tmp_path, speech_server, provider)
    engine.design('A low voice.', 42)
    engine.speak('Hello.', voice, 42)
    assert [call['model'] for call in speech_server.speeches] == ['breeze']
    assert models.loads == [speech.TRANSCRIBER, speech.PROVIDERS[provider].models[0]]


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
    assert engine.chatterbox is engine.multilingual is engine.designer is engine.breeze is engine.narrator is None


PINNED_IDENTITY = {
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
}


@pytest.mark.parametrize('provider', list(speech.PROVIDERS))
def test_generation_identity_is_pinned_and_does_not_depend_on_where_the_models_run(tmp_path, provider):
    with_service = speech.Engine(tmp_path / 'hub', provider, tts_url='http://127.0.0.1:9', tts_consent=CONSENT)
    assert with_service.generation_identity == PINNED_IDENTITY[provider]
    if provider != 'breeze-gguf':
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
