"""Speech service for me-read: Breeze, Qwen voice design and Chatterbox behind one HTTP API. It runs on the machine with the GPU."""
import argparse
import base64
import binascii
import gc
import io
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

log = logging.getLogger('speech')
NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
DISCLAIMER = 'X-Crispasr-Spoken-Disclaimer'
UPLOAD_LIMIT = 50 * 1024 * 1024
# Chatterbox Multilingual's language ids; English goes to Turbo.
CHATTERBOX_LANGUAGES = {'ar', 'da', 'de', 'el', 'en', 'es', 'fi', 'fr', 'he', 'hi', 'it', 'ja', 'ko', 'ms', 'nl', 'no',
                        'pl', 'pt', 'ru', 'sv', 'sw', 'tr', 'zh'}


class Problem(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Speech(BaseModel):
    model: str
    input: str = Field(min_length=1, max_length=5000)
    voice: str | None = None
    ref_text: str | None = None
    instructions: str | None = None
    cfg_scale: float | None = Field(None, gt=0, le=20)
    language: str | None = Field(None, pattern=r'^[a-z]{2,3}$')
    seed: int = Field(0, ge=0, lt=2 ** 63)
    consent_attestation: str | None = None
    spoken_disclaimer: bool = True
    response_format: str = Field('wav', pattern=r'^wav$')


class Speaker(BaseModel):
    audio: str = Field(min_length=1)
    language: str = Field('en', pattern=r'^[a-z]{2,3}$')


@dataclass(frozen=True)
class Voice:
    name: str
    wav: Path
    transcript: str | None


class Voices:
    def __init__(self, folder):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def find(self, name):
        wav, text = self.folder / f'{name}.wav', self.folder / f'{name}.txt'
        if not wav.is_file():
            # The old CrispASR server answered an unknown voice this way, and me-read re-uploads the voice on it.
            raise Problem(500, 'synthesis_failed', f'the voice {name} is not uploaded')
        return Voice(name, wav, text.read_text() if text.is_file() else None)

    def save(self, name, wav, transcript):
        """Saves the voice; False when the name is taken. The WAV appears last, so a voice's text is there before it."""
        final = self.folder / f'{name}.wav'
        if final.exists():
            return False
        staged = self.folder / f'.{name}.{os.getpid()}.{threading.get_ident()}'
        if transcript:
            text = staged.with_suffix('.txt')
            text.write_text(transcript)
            text.replace(self.folder / f'{name}.txt')
        staged.write_bytes(wav)
        try:
            os.link(staged, final)
        except FileExistsError:
            return False
        finally:
            staged.unlink()
        return True


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Child:
    """A CrispASR server process with one model loaded, on a free localhost port."""
    START_TIMEOUT = 300
    SPEECH_TIMEOUT = 900

    def __init__(self, argv, env):
        self.port = free_port()
        self.process = subprocess.Popen([*argv, '--host', '127.0.0.1', '--port', str(self.port)], env=env)
        try:
            self._wait_ready()
        except BaseException:
            self.stop()
            raise

    def _wait_ready(self):
        deadline = time.monotonic() + self.START_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'CrispASR exited with code {self.process.returncode} while starting')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/health', timeout=2) as reply:
                    if reply.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.5)
        raise RuntimeError(f'CrispASR did not answer /health within {self.START_TIMEOUT} seconds')

    def alive(self):
        return self.process.poll() is None

    def speak(self, request, voice):
        body = {'input': request.input, 'response_format': 'wav', 'seed': request.seed,
                'spoken_disclaimer': request.spoken_disclaimer}
        for key in ('instructions', 'cfg_scale', 'language'):
            if getattr(request, key) is not None:
                body[key] = getattr(request, key)
        if voice:
            body |= {'voice': voice.name, 'ref_text': request.ref_text or voice.transcript,
                     'consent_attestation': request.consent_attestation,
                     'marking_attestation': request.consent_attestation}
        call = urllib.request.Request(f'http://127.0.0.1:{self.port}/v1/audio/speech', data=json.dumps(body).encode(),
                                      headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(call, timeout=self.SPEECH_TIMEOUT) as reply:
                return reply.read(), reply.headers.get(DISCLAIMER)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read())['error']
                code, message = detail.get('code') or detail.get('type') or 'synthesis_failed', detail.get('message', '')
            except (ValueError, KeyError, TypeError, AttributeError):
                code, message = 'synthesis_failed', f'CrispASR answered HTTP {e.code}'
            raise Problem(e.code, code, message) from None
        except OSError as e:
            raise Problem(503, 'engine_failed', f'CrispASR did not answer: {e}') from None

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class Cloner:
    """Chatterbox Turbo or Multilingual in this process. Conditionals are cached per voice."""

    def __init__(self, folder, turbo):
        import torch
        self.torch, self.turbo, self.conds = torch, turbo, {}
        if turbo:
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            self.model = ChatterboxTurboTTS.from_local(folder, device='cuda')
        else:
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            self.model = ChatterboxMultilingualTTS.from_local(folder, device='cuda', t3_model='v3')

    def alive(self):
        return True

    def speak(self, request, voice):
        torch, model = self.torch, self.model
        log.info('chatterbox clone of %s, consent: %r', voice.name, request.consent_attestation)
        with torch.inference_mode():
            if voice.name not in self.conds:
                if self.turbo:
                    model.prepare_conditionals(str(voice.wav), exaggeration=0, norm_loudness=True)
                else:
                    model.prepare_conditionals(str(voice.wav))
                self.conds[voice.name] = model.conds
            model.conds = self.conds[voice.name]
            torch.manual_seed(request.seed)
            if self.turbo:
                wav = model.generate(request.input, temperature=0.8, top_p=0.95, top_k=1000, repetition_penalty=1.2)
            else:
                wav = model.generate(request.input, language_id=request.language)
        out = io.BytesIO()
        sf.write(out, wav.reshape(-1).float().cpu().numpy(), model.sr, format='WAV', subtype='PCM_16')
        return out.getvalue(), 'skipped'

    def speaker(self, audio, rate):
        import librosa
        heard = librosa.resample(audio, orig_sr=rate, target_sr=16000) if rate != 16000 else audio
        with self.torch.inference_mode():
            embeds = np.asarray(self.model.ve.embeds_from_wavs([heard], sample_rate=16000))
        mean = embeds.reshape(-1, embeds.shape[-1]).mean(axis=0).astype('float64')
        return (mean / np.linalg.norm(mean)).tolist()

    def stop(self):
        self.model = self.conds = None
        gc.collect()
        self.torch.cuda.empty_cache()


@dataclass(frozen=True)
class Engine:
    gigabytes: float
    load: Callable[[], Child | Cloner]


def engines(args):
    models = args.models
    codec = str(models / 'qwen3-tts-tokenizer-12hz.gguf')
    env = dict(os.environ)
    if args.crispasr_library_path:
        env['LD_LIBRARY_PATH'] = args.crispasr_library_path
    # Every child shares the voice store, so an uploaded voice is visible to all of them at once.
    common = [str(args.crispasr), '--server', '--codec-model', codec, '--voice-dir', str(args.voices),
              '--accept-marking-responsibility']
    return {
        'breeze': Engine(3.3, lambda: Child([*common, '--backend', 'bt2-tts', '-m', str(models / 'breeze-tts-2-q4_k.gguf'),
                                             '--accept-license', 'other'], env)),
        'qwen': Engine(2.9, lambda: Child([*common, '--backend', 'qwen3-tts-1.7b-voicedesign',
                                           '-m', str(models / 'qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf')], env)),
        'chatterbox-turbo': Engine(4.5, lambda: Cloner(models / 'chatterbox-turbo', turbo=True)),
        'chatterbox-multilingual': Engine(5.5, lambda: Cloner(models / 'chatterbox-multilingual', turbo=False)),
    }


def required_files(args):
    return [args.crispasr, *(args.models / name for name in (
        'breeze-tts-2-q4_k.gguf', 'qwen3-tts-tokenizer-12hz.gguf', 'qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf',
        'chatterbox-turbo/t3_turbo_v1.safetensors', 'chatterbox-multilingual/t3_mtl23ls_v3.safetensors'))]


def chatterbox_engine(language):
    return 'chatterbox-turbo' if language == 'en' else 'chatterbox-multilingual'


@dataclass(frozen=True)
class Rules:
    """What a request `model` accepts and which engine answers it."""
    voice: str
    instructions: str
    cfg_scale: bool
    engine: Callable[[Speech], str]
    # Without a transcript CrispASR would guess one with its own small Whisper.
    voice_needs_transcript: bool = False


MODELS = {
    'breeze': Rules(voice='optional', instructions='optional', cfg_scale=True, engine=lambda r: 'breeze',
                    voice_needs_transcript=True),
    'qwen': Rules(voice='forbidden', instructions='required', cfg_scale=False, engine=lambda r: 'qwen'),
    'chatterbox': Rules(voice='required', instructions='forbidden', cfg_scale=False,
                        engine=lambda r: chatterbox_engine(r.language)),
}


def check(request):
    rules = MODELS.get(request.model)
    if rules is None:
        raise Problem(400, 'unknown_model', f'model must be one of {", ".join(MODELS)}')
    for field in ('voice', 'instructions'):
        given, rule = getattr(request, field) not in (None, ''), getattr(rules, field)
        if rule == 'required' and not given:
            raise Problem(400, 'missing_required_field', f'{request.model} needs {field}')
        if rule == 'forbidden' and given:
            raise Problem(400, 'invalid_request', f'{request.model} does not take {field}')
    if request.cfg_scale is not None and not rules.cfg_scale:
        raise Problem(400, 'invalid_request', f'{request.model} does not take cfg_scale')
    if request.voice is not None:
        if not NAME.fullmatch(request.voice):
            raise Problem(400, 'invalid_voice', 'voice must be 1 to 64 letters, digits, - or _')
        if not (request.consent_attestation or '').strip():
            raise Problem(400, 'consent_required', 'a request that names a voice needs consent_attestation')
    if request.model == 'chatterbox':
        request.language = request.language or 'en'
        if request.language not in CHATTERBOX_LANGUAGES:
            raise Problem(400, 'unsupported_language', f'chatterbox speaks {", ".join(sorted(CHATTERBOX_LANGUAGES))}')
    return rules


@dataclass
class Pending:
    name: str
    thread: threading.Thread
    outcome: dict


class Loader:
    """Loads engines on first use and unloads the least recently used until the estimated total fits the budget.

    The caller holds `lock` for the whole request, so one request runs at a time. At most one load runs; a request that
    waits on it longer than `wait` seconds gets a 503 and the load carries on.
    """

    def __init__(self, engines, budget, wait):
        self.engines, self.budget, self.wait = engines, budget, wait
        self.lock = threading.Lock()
        self.ready = OrderedDict()
        self.pending = None

    def get(self, name):
        self._settle(name)
        loaded = self.ready.get(name)
        if loaded is not None and not loaded.alive():
            log.warning('%s stopped by itself; starting it again', name)
            self.ready.pop(name).stop()
            loaded = None
        if loaded is not None:
            self.ready.move_to_end(name)
            return loaded
        needed = self.engines[name].gigabytes
        while self.ready and sum(self.engines[n].gigabytes for n in self.ready) + needed > self.budget:
            evicted, engine = self.ready.popitem(last=False)
            log.info('unloading %s to make room for %s', evicted, name)
            engine.stop()
        log.info('loading %s', name)
        outcome = {}
        thread = threading.Thread(target=self._load, args=(name, outcome), name=f'load-{name}', daemon=True)
        self.pending = Pending(name, thread, outcome)
        thread.start()
        self._settle(name)
        return self.ready[name]

    def _load(self, name, outcome):
        start = time.perf_counter()
        try:
            outcome['loaded'] = self.engines[name].load()
            log.info('%s loaded in %.1f s', name, time.perf_counter() - start)
        except Exception as e:
            log.exception('could not load %s', name)
            outcome['error'] = str(e)[:300]

    def _settle(self, wanted):
        pending = self.pending
        if pending is None:
            return
        pending.thread.join(self.wait)
        if pending.thread.is_alive():
            raise Problem(503, 'loading', f'{pending.name} is still loading; retry shortly')
        self.pending = None
        if 'loaded' in pending.outcome:
            self.ready[pending.name] = pending.outcome['loaded']
        elif pending.name == wanted:
            raise Problem(500, 'load_failed', f'{pending.name} did not load: {pending.outcome.get("error")}')

    def status(self):
        pending = self.pending
        return {'ready': True, 'loaded': list(self.ready),
                'loading': pending.name if pending and pending.thread.is_alive() else None}

    def close(self):
        if self.pending:
            self.pending.thread.join()
            if 'loaded' in self.pending.outcome:
                self.ready[self.pending.name] = self.pending.outcome['loaded']
        for engine in self.ready.values():
            engine.stop()
        self.ready.clear()


def error(status, code, message):
    return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status)


def decode_audio(encoded):
    try:
        audio, rate = sf.read(io.BytesIO(base64.b64decode(encoded, validate=True)), dtype='float32', always_2d=True)
    except (binascii.Error, sf.LibsndfileError, RuntimeError) as e:
        raise Problem(400, 'invalid_audio', 'audio is not a base64 encoded WAV') from e
    if not len(audio):
        raise Problem(400, 'invalid_audio', 'audio is empty')
    return audio.mean(axis=1), rate


def create_app(loader, voices):
    @asynccontextmanager
    async def lifespan(app):
        yield
        loader.close()

    app = FastAPI(title='me-read speech', lifespan=lifespan)

    @app.exception_handler(Problem)
    async def problem(_, e):
        return error(e.status, e.code, e.message)

    @app.exception_handler(RequestValidationError)
    async def invalid(_, e):
        first = e.errors()[0]
        return error(400, 'invalid_request', f'{".".join(map(str, first["loc"][1:]))}: {first["msg"]}')

    @app.exception_handler(HTTPException)
    async def http(_, e):
        return error(e.status_code, 'not_found' if e.status_code == 404 else 'http_error', str(e.detail))

    @app.get('/health')
    def health():
        return loader.status()

    @app.post('/v1/voices', status_code=201)
    def upload(voice: UploadFile = File(), name: str = Form(), consent_attestation: str = Form(''),
               transcript: str = Form('')):
        if not NAME.fullmatch(name):
            raise Problem(400, 'invalid_name', 'name must be 1 to 64 letters, digits, - or _')
        if not consent_attestation.strip():
            raise Problem(400, 'consent_required', 'uploading a voice needs consent_attestation')
        wav = voice.file.read(UPLOAD_LIMIT + 1)
        if len(wav) > UPLOAD_LIMIT:
            raise Problem(413, 'too_large', f'a voice must be at most {UPLOAD_LIMIT} bytes')
        try:
            info = sf.info(io.BytesIO(wav))
        except (sf.LibsndfileError, RuntimeError):
            raise Problem(400, 'invalid_audio', 'voice is not a WAV file') from None
        if info.format != 'WAV' or info.frames == 0:
            raise Problem(400, 'invalid_audio', 'voice is not a WAV file')
        if not voices.save(name, wav, transcript.strip()):
            raise Problem(409, 'voice_exists', f'the voice {name} is already uploaded')
        log.info('saved voice %s, consent: %r', name, consent_attestation)
        return {'name': name}

    @app.post('/v1/audio/speech')
    def speech(request: Speech):
        rules = check(request)
        voice = voices.find(request.voice) if request.voice else None
        if voice and rules.voice_needs_transcript and not (request.ref_text or voice.transcript):
            raise Problem(400, 'ref_text_required', f'{request.model} needs ref_text, or a transcript uploaded with the voice')
        name = rules.engine(request)
        with loader.lock:
            engine = loader.get(name)
            start = time.perf_counter()
            try:
                wav, disclaimer = engine.speak(request, voice)
            except Problem:
                raise
            except Exception as e:
                log.exception('%s failed', name)
                raise Problem(500, 'synthesis_failed', str(e)[:300]) from None
            elapsed = time.perf_counter() - start
        if voice and disclaimer and disclaimer.strip().lower() == 'applied':
            raise Problem(502, 'disclaimer_applied', 'CrispASR put its spoken AI disclaimer into the audio')
        seconds = sf.info(io.BytesIO(wav)).duration
        log.info('%s: %.1f s of audio in %.1f s (%.2f s per s)', name, seconds, elapsed, elapsed / max(seconds, 1e-3))
        return Response(wav, media_type='audio/wav', headers={DISCLAIMER: disclaimer} if disclaimer else None)

    @app.post('/v1/speaker')
    def speaker(request: Speaker):
        if request.language not in CHATTERBOX_LANGUAGES:
            raise Problem(400, 'unsupported_language', f'chatterbox knows {", ".join(sorted(CHATTERBOX_LANGUAGES))}')
        audio, rate = decode_audio(request.audio)
        with loader.lock:
            return {'embedding': loader.get(chatterbox_engine(request.language)).speaker(audio, rate)}

    return app


def main():
    home = Path.home() / 'me-read-speech'
    parser = argparse.ArgumentParser(description=__doc__)
    # There is no login: anyone who can reach the port can use the GPU and upload voices, so keep it on localhost.
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8781)
    parser.add_argument('--voices', type=Path, default=home / 'voices', help='folder of uploaded voices')
    parser.add_argument('--models', type=Path, default=home / 'models', help='folder of model files, see README')
    parser.add_argument('--crispasr', type=Path, default=home / 'build/bin/crispasr', help='the CrispASR binary')
    parser.add_argument('--crispasr-library-path', default='',
                        help='LD_LIBRARY_PATH for CrispASR only; PyTorch brings its own ROCm libraries')
    parser.add_argument('--budget-gb', type=float, default=13, help='estimated GPU memory the loaded models may use')
    parser.add_argument('--load-wait', type=float, default=60,
                        help='seconds a request waits for a model to load before it gets a 503')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    missing = [str(path) for path in required_files(args) if not path.exists()]
    if missing:
        parser.error('missing: ' + ', '.join(missing))
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    # MIOpen otherwise searches for kernels for every new sentence length, which made Chatterbox about three times
    # slower on lines it had not seen (Turbo 0.96 against 0.28 seconds of work per second of audio).
    os.environ.setdefault('MIOPEN_FIND_MODE', 'FAST')
    loader = Loader(engines(args), args.budget_gb, args.load_wait)
    uvicorn.run(create_app(loader, Voices(args.voices)), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
