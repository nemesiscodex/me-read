"""Local speech: design a 15 second voice sample from a description, direct it into emotion takes, and speak text in
a sampled voice."""
import hashlib
import http.client
import io
import json
import math
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

import numpy as np
import soundfile as sf

from .emotions import DIRECTIONS

CHATTERBOX = 'mlx-community/chatterbox-turbo-8bit'
TOKENIZER = 'mlx-community/S3TokenizerV2'
DESIGNER = 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'
MULTILINGUAL = 'mlx-community/chatterbox-8bit'
BREEZE = 'mlx-community/Breeze-TTS-2-mlx-4bit'
TRANSCRIBER = 'mlx-community/whisper-large-v3-turbo-asr-fp16'
TRANSCRIBER_REVISION = '624c19c9af5603fa73b83bce14d4aeea96156d18'
TRANSCRIPTION_POLICY = 'whisper-auto-language-16khz-v1'
CACHE_SIZE = 24


@dataclass(frozen=True)
class Provider:
    name: str
    models: tuple[str, ...]
    languages: tuple[str, ...] | None
    reference: str

    @property
    def identity(self):
        models = [{'id': model} for model in (*self.models, DESIGNER)]
        if self.reference != 'conditionals':
            models.append({'id': TRANSCRIBER, 'revision': TRANSCRIBER_REVISION})
        return {'provider': self.name, 'models': models, 'policy': 1,
                **({'transcription_policy': TRANSCRIPTION_POLICY} if self.reference != 'conditionals' else {}),
                **({'spoken_disclaimer': False} if self.reference == 'remote' else {})}

    def validate_language(self, language):
        if self.languages is None or language in self.languages:
            return
        name = self.name.capitalize()
        if language == 'es':
            raise ValueError(f'{name} narrates English only. For Spanish, restart with --tts-provider chatterbox '
                             'or --tts-provider fish.')
        raise ValueError(f'{name} narrates English only, and this book is in {language}.')


PROVIDERS = {
    'chatterbox': Provider('chatterbox', (CHATTERBOX, MULTILINGUAL, TOKENIZER), None, 'conditionals'),
    'fish': Provider('fish', ('mlx-community/fish-audio-s2-pro-8bit',), None, 'waveform'),
    'breeze': Provider('breeze', (BREEZE,), ('en',), 'path'),
    'breeze-gguf': Provider('breeze-gguf', ('cstr/breeze-tts-2-GGUF@q4_k',), ('en',), 'remote'),
}


@dataclass(frozen=True)
class Reference:
    wav: bytes
    text: str
    waveform: object = None

# What the voice designer reads for the 15 second sample, so the sample is in the language it will be cloned for.
SAMPLE_TEXT = {
    'en': ('English', 'I was not sure what I would find when I opened the door that morning. The garden was quiet, '
                      'and the path ran down toward the river. Have you ever seen it in the early light? '
                      'Nobody spoke for a while, and then we all began to laugh.'),
    'es': ('Spanish', 'No sabía qué iba a encontrar cuando abrí la puerta aquella mañana. El jardín estaba en silencio, '
                      'y el sendero bajaba hacia el río. ¿Lo has visto alguna vez con la primera luz? '
                      'Nadie habló durante un rato, y luego todos empezamos a reír.'),
}
SAMPLE_SECONDS = 15
MIN_SAMPLE_SECONDS = 6
AGE = {'child': 'A young child', 'teen': 'A teenager', 'adult': 'An adult', 'elderly': 'An elderly person'}
AGE_GENDER = {('child', 'male'): 'A little boy', ('child', 'female'): 'A little girl',
              ('teen', 'male'): 'A teenage boy', ('teen', 'female'): 'A teenage girl',
              ('adult', 'male'): 'An adult man', ('adult', 'female'): 'An adult woman',
              ('elderly', 'male'): 'An elderly man', ('elderly', 'female'): 'An elderly woman'}
# The voice designers give "a teenage boy" a woman's pitch more often than not; a young man of eighteen stays a man.
# Portraits still show a teenage boy.
VOICE_AGE_GENDER = {**AGE_GENDER, ('teen', 'male'): 'A young man of about eighteen'}
PITCH = {'low': 'a low, deep voice', 'medium': 'a medium-pitched voice', 'high': 'a high, bright voice'}
# Semitones a new sample is moved by when it sounds like a voice the book already has, mildest first.
SHIFTS = 0, -2, 2, -3, 3
# How alike two samples are to the cloning model, from 0 to 1, above which they pass for one person. Two samples
# designed from the same description score about 0.9, two different men about 0.7.
SAME_VOICE = 0.85
# The highest median pitch, in Hz, a man's or older boy's voice may have at each pitch. The designers sometimes give
# them a woman's voice, which sits above 220 Hz.
MALE_CEILING = {'low': 160, 'medium': 210, 'high': 250}
DESIGN_TRIES = 4


def voice_instruction(character):
    """The plain-language voice description the designer model works from."""
    who = VOICE_AGE_GENDER.get((character['age'], character['gender']), AGE[character['age']])
    detail = character['description'].strip().rstrip('.')
    # Short and about the sound only: with a sentence every voice shares, the designer returns much the same voice.
    return f"{who} with {PITCH[character['pitch']]}." + (f' {detail[0].upper()}{detail[1:]}.' if detail else '')


def pitch_ceiling(character):
    """The highest median pitch the character's designed voice may have, or None when any pitch will do."""
    if character['gender'] != 'male' or character['age'] == 'child':
        return None
    return MALE_CEILING[character['pitch']]


def median_pitch(audio, rate):
    """The median pitch of the voiced parts of `audio` in Hz, or None when nothing in it is voiced."""
    hop, window, found = rate // 100, rate // 25, []
    for start in range(0, len(audio) - window, hop):
        frame = audio[start:start + window] - audio[start:start + window].mean()
        if np.sqrt((frame ** 2).mean()) < 0.02:
            continue
        correlation = np.correlate(frame, frame, 'full')[window - 1:]
        lag = rate // 500 + int(np.argmax(correlation[rate // 500:rate // 60]))
        if correlation[lag] > 0.5 * correlation[0]:
            found.append(rate / lag)
    return float(np.median(found)) if found else None


def within_ceiling(tries, make, ceiling):
    """The first sample whose median pitch is at or below `ceiling`, or the lowest of them all. One try without one.

    `make(i)` designs try i and returns (sample, median pitch or None).
    """
    made = []
    for i in range(1 if ceiling is None else tries):
        sample, pitch = make(i)
        if ceiling is None or (pitch is not None and pitch <= ceiling):
            return sample
        made.append((math.inf if pitch is None else pitch, sample))
    return min(made, key=lambda attempt: attempt[0])[1]


def shifted(audio, semitones):
    """The audio played slower or faster, which moves pitch and timbre together, as a larger or smaller speaker would."""
    if not semitones:
        return audio
    return np.interp(np.arange(0, len(audio) - 1, 2 ** (semitones / 12)), np.arange(len(audio)), audio).astype('float32')


def most_distinct(shifts, alike):
    """The first shift at which the sample no longer passes for a voice already in use, or the least alike of them all.

    `alike(shift)` is how alike the shifted sample is to the closest existing voice.
    """
    scores = {}
    for shift in shifts:
        scores[shift] = alike(shift)
        if scores[shift] < SAME_VOICE:
            return shift
    return min(scores, key=scores.get)


def trim(audio, rate, threshold=0.01, before=0.03, after=0.12):
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if not len(loud):
        return audio
    return audio[max(0, loud[0] - int(before * rate)):loud[-1] + 1 + int(after * rate)]


def read_sample(path):
    """Any audio file as (mono float32, rate), cut to the sample length. Raises ValueError when unusable."""
    try:
        audio, rate = sf.read(path, dtype='float32', always_2d=True)
    except Exception:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / 'sample.wav'
            done = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(path), '-ac', '1', '-ar', '24000', str(wav)],
                                  capture_output=True, text=True)
            if done.returncode != 0:
                raise ValueError('could not read that audio file')
            audio, rate = sf.read(wav, dtype='float32', always_2d=True)
    audio = trim(audio.mean(axis=1), rate)[:SAMPLE_SECONDS * rate]
    if len(audio) < MIN_SAMPLE_SECONDS * rate:
        raise ValueError(f'the recording needs at least {MIN_SAMPLE_SECONDS} seconds of speech')
    return audio, rate


class RemoteError(RuntimeError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class Unavailable(RemoteError):
    """The speech server cannot answer now: it is down, unreachable, slow, or still loading. The clip is retried."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would turn the POST into a GET of another page, such as a proxy's login page.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Remote:
    """The speech server on another machine. It answers one request at a time, so the engine calls it one at a time."""
    UPLOAD_TIMEOUT = 60
    SPEECH_TIMEOUT = 300
    REPLY_LIMIT = 64 * 1024 * 1024
    DISCLAIMER = 'X-Crispasr-Spoken-Disclaimer'

    def __init__(self, url, consent):
        if not url:
            raise ValueError('--tts-url is required with --tts-provider breeze-gguf, for example http://HOST:8781')
        if not consent or not consent.strip():
            raise ValueError('--tts-consent is required with --tts-provider breeze-gguf. Write your own statement about '
                             'the reference voices; there is no default.')
        try:
            consent.encode()
        except UnicodeEncodeError:
            raise ValueError('--tts-consent must be plain UTF-8 text') from None
        parts = urllib.parse.urlsplit(url.strip())
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('--tts-url must start with http:// or https:// and name a host')
        if '@' in parts.netloc:
            raise ValueError('--tts-url must not contain a login; leave out the part before the @')
        if parts.query or parts.fragment:
            raise ValueError('--tts-url must not contain a query or fragment')
        path = parts.path[:-1] if parts.path.endswith('/') else parts.path
        self.host, self.base, self.consent = parts.netloc, f'{parts.scheme}://{parts.netloc}{path}', consent
        self.opener = urllib.request.build_opener(NoRedirect)

    def upload(self, name, wav, transcript):
        boundary = uuid.uuid4().hex
        body = form(boundary, [('voice', wav, 'reference.wav'), ('name', name.encode(), None),
                               ('consent_attestation', self.consent.encode(), None),
                               ('transcript', transcript.encode(), None)])
        request = urllib.request.Request(f'{self.base}/v1/voices', data=body, method='POST',
                                         headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        try:
            status, _, _ = self._call(request, self.UPLOAD_TIMEOUT)
        except RemoteError as e:
            if e.status != 409:
                raise
            return
        if status != 201:
            raise RemoteError(f'The speech server at {self.host} did not confirm the voice upload (HTTP {status}).')

    def speech(self, text, voice, transcript, seed):
        body = json.dumps({'input': text, 'voice': voice, 'ref_text': transcript, 'seed': seed, 'response_format': 'wav',
                           'consent_attestation': self.consent, 'spoken_disclaimer': False}).encode()
        request = urllib.request.Request(f'{self.base}/v1/audio/speech', data=body, method='POST',
                                         headers={'Content-Type': 'application/json'})
        status, headers, wav = self._call(request, self.SPEECH_TIMEOUT)
        if status != 200:
            raise RemoteError(f'The speech server at {self.host} answered HTTP {status} instead of audio.')
        state = headers.get(self.DISCLAIMER)
        if state is None:
            raise RemoteError(f'The speech server at {self.host} did not say whether it left its spoken disclaimer out, '
                              'so the disclaimer state could not be confirmed.')
        state = state.strip().lower()
        if state == 'applied':
            raise RemoteError(f'The speech server at {self.host} put its spoken AI disclaimer into the audio, because it '
                              'was started without --accept-marking-responsibility. Start the speech server with '
                              '--accept-marking-responsibility.')
        if state != 'skipped':
            raise RemoteError(f'The speech server at {self.host} reported an unknown spoken disclaimer state.')
        return wav

    def _call(self, request, timeout):
        try:
            with self.opener.open(request, timeout=timeout) as reply:
                body = reply.read(self.REPLY_LIMIT + 1)
                if len(body) > self.REPLY_LIMIT:
                    raise RemoteError(f'The speech server at {self.host} sent a reply larger than {self.REPLY_LIMIT} bytes.')
                return reply.status, reply.headers, body
        except urllib.error.HTTPError as e:
            code, message = problem(e)
            text = f'The speech server at {self.host} answered HTTP {e.code}'
            if code:
                text += f' {code}'
            if message:
                text += f': {message}'
            cls = Unavailable if e.code == 503 else RemoteError
            raise cls(text + '.', e.code, code) from None
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, TimeoutError) or isinstance(getattr(e, 'reason', None), TimeoutError):
                raise Unavailable(f'The speech server at {self.host} did not answer within {timeout} seconds.') from None
            raise Unavailable(f'Could not reach the speech server at {self.host}. Check that it is running, that the '
                              'address is right, and that this machine can reach it.') from None


def form(boundary, fields):
    """multipart/form-data bytes for (name, value, filename or None) fields."""
    body = b''
    for name, value, filename in fields:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else '')
        kind = 'Content-Type: audio/wav\r\n' if filename else ''
        body += f'--{boundary}\r\nContent-Disposition: {disposition}\r\n{kind}\r\n'.encode() + value + b'\r\n'
    return body + f'--{boundary}--\r\n'.encode()


def problem(error):
    """The (code, message) in a server's JSON error body, or (None, None) when it has none."""
    try:
        detail = json.loads(error.read())['error']
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(detail, dict):
        return None, None
    return detail.get('code'), str(detail.get('message', ''))[:200]


def mono(samples, rate):
    """Mono float32 samples and their integral rate. Raises RuntimeError when the model output cannot be used."""
    if samples.ndim == 2 and samples.shape[0] == 1:
        samples = samples[0]
    if samples.ndim != 1 or samples.dtype.kind not in 'fiu':
        raise RuntimeError('the speech model returned invalid mono audio')
    if not len(samples):
        raise RuntimeError('the speech model returned no audio')
    if not np.isfinite(samples).all() or (np.abs(samples) > np.finfo(np.float32).max).any():
        raise RuntimeError('the speech model returned invalid mono audio')
    if (not isinstance(rate, Real) or isinstance(rate, bool) or not math.isfinite(rate)
            or rate <= 0 or int(rate) != rate):
        raise RuntimeError('the speech model returned an invalid sample rate')
    return samples.astype('float32'), int(rate)


def decode_wav(data):
    try:
        audio, rate = sf.read(io.BytesIO(data), dtype='float32', always_2d=True)
    except (RuntimeError, ValueError):
        raise RuntimeError('the speech server returned audio that is not a readable WAV') from None
    return mono(audio.T, rate)


class Engine:
    """A selected local narrator, Breeze to design English voices and direct takes, and the Qwen3 voice designer for
    other languages. One worker owns all model calls."""

    def __init__(self, models_dir, tts_provider='chatterbox', tts_url=None, tts_consent=None):
        self.provider = PROVIDERS[tts_provider]
        self.remote = Remote(tts_url, tts_consent) if self.provider.reference == 'remote' else None
        self.hub = Path(models_dir)
        self.hub.mkdir(parents=True, exist_ok=True)
        os.environ['HF_HUB_CACHE'] = str(self.hub)
        os.environ.setdefault('TQDM_DISABLE', '1')
        os.environ.setdefault('TRANSFORMERS_VERBOSITY', 'error')
        self.chatterbox = self.multilingual = self.designer = self.narrator = self.breeze = None
        self.voices, self.speakers, self.transcripts, self.uploads = {}, {}, {}, {}

    @property
    def generation_identity(self):
        return self.provider.identity

    def _snapshot(self, repo, revision=None):
        from huggingface_hub import snapshot_download
        options = {'revision': revision} if revision else {}
        try:
            return snapshot_download(repo, cache_dir=self.hub, local_files_only=True, **options)
        except Exception:
            return snapshot_download(repo, cache_dir=self.hub, **options)

    def _generate(self, results, rate):
        import mlx.core as mx
        parts, actual_rate = [], None
        try:
            for result in results:
                mx.eval(result.audio)
                samples, reported = mono(np.asarray(result.audio), getattr(result, 'sample_rate', rate))
                if actual_rate is not None and reported != actual_rate:
                    raise RuntimeError('the speech model returned inconsistent sample rates')
                actual_rate = reported
                parts.append(samples)
            if not parts:
                raise RuntimeError('the speech model returned no audio')
            return np.concatenate(parts), actual_rate
        finally:
            mx.clear_cache()

    def _breeze(self):
        from mlx_audio.tts.utils import load_model
        # The Breeze narrator designs and directs too; a second copy would only take memory.
        if self.provider.models[0] == BREEZE:
            if self.narrator is None:
                self.narrator = load_model(self._snapshot(BREEZE))
            return self.narrator
        if self.breeze is None:
            self.breeze = load_model(self._snapshot(BREEZE))
        return self.breeze

    def design(self, instruction, seed, language='en', others=(), ceiling=None):
        """A voice sample of about 15 seconds matching `instruction`, spoken in `language`, as (audio, rate). Breeze
        designs English voices and Qwen3 the others.

        `others` are the samples of the voices already in use. The designer tends to return the same few voices, so a
        sample the cloning model would take for one of them is shifted until it is not. A sample whose median pitch is
        above `ceiling` is designed again with the next seed, up to four tries, keeping the lowest.
        """
        def attempt(i):
            audio, rate = self._designed(instruction, seed + i, language)
            audio = trim(audio, rate)[:SAMPLE_SECONDS * rate]
            if len(audio) < MIN_SAMPLE_SECONDS * rate:
                raise RuntimeError('the designed voice sample came out too short')
            audio = self._apart(audio, rate, [path for path in others if os.path.exists(path)], language)
            return (audio, rate), (None if ceiling is None else median_pitch(audio, rate))
        return within_ceiling(DESIGN_TRIES, attempt, ceiling)

    def _designed(self, instruction, seed, language):
        import mlx.core as mx
        mx.random.seed(seed)
        spoken, text = SAMPLE_TEXT.get(language, SAMPLE_TEXT['en'])
        if language == 'en':
            designer = self._breeze()
            return self._generate(designer.generate(text=text, instruct=instruction, cfg_scale=4.0, seed=seed, stream=False),
                                  designer.sample_rate)
        from mlx_audio.tts.utils import load_model
        if self.designer is None:
            self.designer = load_model(self._snapshot(DESIGNER))
        return self._generate(self.designer.generate_voice_design(text=text, instruct=instruction, language=spoken),
                              self.designer.sample_rate)

    def direct(self, voice_path, emotion, seed):
        """A take of about 15 seconds of the saved voice saying the emotion's script the way the emotion asks, as
        (audio, rate). Breeze directs it on this Mac whichever narrator reads the book."""
        import mlx.core as mx
        instruction, script = DIRECTIONS[emotion]
        wav = Path(voice_path).read_bytes()
        director = self._breeze()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'reference.wav'
            path.write_bytes(wav)
            transcript = self._transcript(path, hashlib.sha256(wav).hexdigest())
            mx.random.seed(seed)
            results = director.generate(text=script, instruct=instruction, ref_audio=str(path), ref_text=transcript,
                                        cfg_scale=4.0, seed=seed, stream=False)
            audio, rate = self._generate(results, director.sample_rate)
        audio = trim(audio, rate)[:SAMPLE_SECONDS * rate]
        if len(audio) < MIN_SAMPLE_SECONDS * rate:
            raise RuntimeError(f'the {emotion} take came out too short')
        return audio, rate

    def _cloner(self, language):
        from mlx_audio.tts.utils import load_model
        if language == 'en':
            if self.chatterbox is None:
                self._snapshot(TOKENIZER)
                self.chatterbox = load_model(self._snapshot(CHATTERBOX))
            return self.chatterbox
        if self.multilingual is None:
            self._snapshot(TOKENIZER)
            self.multilingual = load_model(self._snapshot(MULTILINGUAL))
        return self.multilingual

    def _speaker(self, model, audio, rate):
        """Who the cloning model hears in the audio, as a unit vector."""
        from mlx_audio.utils import resample_audio
        heard = np.asarray(model.ve.embeds_from_wavs([resample_audio(audio, rate, 16000)], sample_rate=16000))
        heard = heard.mean(axis=0).flatten().astype('float64')
        return heard / np.linalg.norm(heard)

    def _apart(self, audio, rate, others, language):
        if not others:
            return audio
        model = self._cloner(language)
        known = []
        for path in others:
            wav = Path(path).read_bytes()
            key = (language == 'en', hashlib.sha256(wav).hexdigest())

            def prepare():
                other, other_rate = sf.read(io.BytesIO(wav), dtype='float32', always_2d=True)
                return self._speaker(model, other.mean(axis=1), other_rate)
            known.append(self._cached(self.speakers, key, prepare))
        # A shift up shortens the sample, and the cloning model needs a few seconds of it.
        shifts = [s for s in SHIFTS if len(audio) / 2 ** (s / 12) >= MIN_SAMPLE_SECONDS * rate]
        shift = most_distinct(shifts, lambda s: float((np.array(known) @ self._speaker(model, shifted(audio, s), rate)).max()))
        return shifted(audio, shift)

    def _cached(self, cache, key, prepare):
        if key not in cache:
            value = prepare()
            if len(cache) >= CACHE_SIZE:
                cache.pop(next(iter(cache)))
            cache[key] = value
        value = cache.pop(key)
        cache[key] = value
        return value

    def _voice(self, model, voice_path, prepare):
        wav = Path(voice_path).read_bytes()
        checksum = hashlib.sha256(wav).hexdigest()

        def condition():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'reference.wav'
                path.write_bytes(wav)
                return prepare(path)
        return self._cached(self.voices, (model, checksum), condition)

    def _transcript(self, path, checksum):
        import mlx.core as mx
        from mlx_audio.stt import load
        from mlx_audio.stt.utils import load_audio

        def transcribe():
            model = None
            try:
                audio = load_audio(str(path), sr=16000)
                model = load(self._snapshot(TRANSCRIBER, TRANSCRIBER_REVISION))
                text = model.generate(audio).text.strip()
                if not text:
                    raise RuntimeError('Whisper returned no words')
                return text
            except Exception as e:
                raise RuntimeError('Could not transcribe the saved reference WAV locally. '
                                   'Check that the recording contains clear speech and the Whisper model is available, '
                                   'then regenerate the clip. ' + str(e)) from e
            finally:
                del model
                mx.clear_cache()
        return self._cached(self.transcripts, (checksum, TRANSCRIBER, TRANSCRIBER_REVISION, TRANSCRIPTION_POLICY), transcribe)

    def _reference(self, voice_path):
        from mlx_audio.tts.utils import load_model
        from mlx_audio.utils import load_audio

        wav = Path(voice_path).read_bytes()
        checksum = hashlib.sha256(wav).hexdigest()

        def prepare():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'reference.wav'
                path.write_bytes(wav)
                text = self._transcript(path, checksum)
                if self.provider.reference == 'remote':
                    return Reference(wav, text)
                if self.narrator is None:
                    self.narrator = load_model(self._snapshot(self.provider.models[0]))
                waveform = load_audio(str(path), sample_rate=self.narrator.sample_rate) if self.provider.reference == 'waveform' else None
                return Reference(wav, text, waveform)
        return self._cached(self.voices, (self.provider.name, self.provider.models, checksum), prepare)

    def _speak_reference(self, text, voice_path, seed):
        import mlx.core as mx
        reference = self._reference(voice_path)
        model = self.narrator
        mx.random.seed(seed)
        if self.provider.reference == 'waveform':
            results = model.generate(text=text, ref_audio=reference.waveform, ref_text=reference.text, stream=False)
            audio, rate = self._generate(results, model.sample_rate)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'reference.wav'
                path.write_bytes(reference.wav)
                results = model.generate(text=text, ref_audio=str(path), ref_text=reference.text, seed=seed, stream=False)
                audio, rate = self._generate(results, model.sample_rate)
        return trim(audio, rate), rate

    def _english(self, voice_path):
        model = self._cloner('en')

        def prepare(path):
            model.prepare_conditionals(str(path), exaggeration=0, norm_loudness=True)
            return model._conds
        model._conds = self._voice('turbo', voice_path, prepare)
        return model, lambda text: model.generate(text=text, temperature=0.8, top_p=0.95, top_k=1000, repetition_penalty=1.2)

    def _other(self, voice_path, language):
        model = self._cloner(language)
        conds = self._voice('multilingual', voice_path, lambda path: model.prepare_conditionals(str(path), 24000))
        return model, lambda text: model.generate(text=text, conds=conds, lang_code=language)

    def _uploaded(self, name, reference):
        def upload():
            self.remote.upload(name, reference.wav, reference.text)
            return True
        self._cached(self.uploads, name, upload)

    def _speak_remote(self, text, voice_path, seed):
        reference = self._reference(voice_path)
        name = 'mr-' + hashlib.sha256(reference.wav).hexdigest()[:32]
        cached = name in self.uploads
        self._uploaded(name, reference)
        try:
            wav = self.remote.speech(text, name, reference.text, seed)
        except RemoteError as e:
            # The server keeps uploaded voices on disk, so this retry covers a voice removed from its voice folder.
            if not cached or e.status != 500 or e.code != 'synthesis_failed':
                raise
            self.uploads.pop(name, None)
            self._uploaded(name, reference)
            wav = self.remote.speech(text, name, reference.text, seed)
        audio, rate = decode_wav(wav)
        return trim(audio, rate), rate

    def speak(self, text, voice_path, seed, language='en'):
        """`text` spoken in the saved sample's voice, as mono float32 samples and their actual rate."""
        self.provider.validate_language(language)
        if self.provider.reference == 'remote':
            return self._speak_remote(text, voice_path, seed)
        if self.provider.reference != 'conditionals':
            return self._speak_reference(text, voice_path, seed)
        import mlx.core as mx
        model, generate = self._english(voice_path) if language == 'en' else self._other(voice_path, language)
        words = len(text.split())
        audio = None
        # A short line sometimes rambles on past its text; a clip far longer than its words is retaken.
        for attempt in range(3):
            mx.random.seed(seed + attempt)
            audio, rate = self._generate(generate(text), model.sample_rate)
            audio = trim(audio, rate)
            if len(audio) / rate <= 2.5 + words * 0.9:
                break
        return audio, rate
