"""Local speech: design a 15 second voice sample from a description, and speak text in a sampled voice."""
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

CHATTERBOX = 'mlx-community/chatterbox-turbo-8bit'
TOKENIZER = 'mlx-community/S3TokenizerV2'
DESIGNER = 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit'
MULTILINGUAL = 'mlx-community/chatterbox-8bit'
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
PITCH = {'low': 'a low, deep voice', 'medium': 'a medium-pitched voice', 'high': 'a high, bright voice'}
# Semitones a new sample is moved by when it sounds like a voice the book already has, mildest first.
SHIFTS = 0, -2, 2, -3, 3
# How alike two samples are to the cloning model, from 0 to 1, above which they pass for one person. Two samples
# designed from the same description score about 0.9, two different men about 0.7.
SAME_VOICE = 0.85


def voice_instruction(character):
    """The plain-language voice description the designer model works from."""
    who = AGE_GENDER.get((character['age'], character['gender']), AGE[character['age']])
    detail = character['description'].strip().rstrip('.')
    # Short and about the sound only: with a sentence every voice shares, the designer returns much the same voice.
    return f"{who} with {PITCH[character['pitch']]}." + (f' {detail[0].upper()}{detail[1:]}.' if detail else '')


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


class Engine:
    """Chatterbox Turbo and the Qwen3 voice designer on MLX. Not thread safe; one worker owns it."""

    def __init__(self, models_dir):
        self.hub = Path(models_dir)
        self.hub.mkdir(parents=True, exist_ok=True)
        os.environ['HF_HUB_CACHE'] = str(self.hub)
        os.environ.setdefault('TQDM_DISABLE', '1')
        os.environ.setdefault('TRANSFORMERS_VERBOSITY', 'error')
        self.chatterbox = self.multilingual = self.designer = None
        self.voices, self.speakers = {}, {}

    def _snapshot(self, repo):
        from huggingface_hub import snapshot_download
        try:
            return snapshot_download(repo, cache_dir=self.hub, local_files_only=True)
        except Exception:
            return snapshot_download(repo, cache_dir=self.hub)

    def _generate(self, results):
        import mlx.core as mx
        parts = []
        for result in results:
            mx.eval(result.audio)
            parts.append(np.asarray(result.audio).flatten())
        mx.clear_cache()
        return np.concatenate(parts).astype('float32')

    def design(self, instruction, seed, language='en', others=()):
        """A voice sample of about 15 seconds matching `instruction`, spoken in `language`, as (audio, rate).

        `others` are the samples of the voices already in use. The designer tends to return the same few voices, so a
        sample the cloning model would take for one of them is shifted until it is not.
        """
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model
        if self.designer is None:
            self.designer = load_model(self._snapshot(DESIGNER))
        mx.random.seed(seed)
        spoken, text = SAMPLE_TEXT.get(language, SAMPLE_TEXT['en'])
        audio = self._generate(self.designer.generate_voice_design(text=text, instruct=instruction, language=spoken))
        rate = self.designer.sample_rate
        audio = trim(audio, rate)[:SAMPLE_SECONDS * rate]
        if len(audio) < MIN_SAMPLE_SECONDS * rate:
            raise RuntimeError('the designed voice sample came out too short')
        return self._apart(audio, rate, [path for path in others if os.path.exists(path)], language), rate

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
            key = (language == 'en', str(path), os.path.getmtime(path))
            if key not in self.speakers:
                other, other_rate = sf.read(path, dtype='float32', always_2d=True)
                self.speakers[key] = self._speaker(model, other.mean(axis=1), other_rate)
            known.append(self.speakers[key])
        # A shift up shortens the sample, and the cloning model needs a few seconds of it.
        shifts = [s for s in SHIFTS if len(audio) / 2 ** (s / 12) >= MIN_SAMPLE_SECONDS * rate]
        shift = most_distinct(shifts, lambda s: float((np.array(known) @ self._speaker(model, shifted(audio, s), rate)).max()))
        return shifted(audio, shift)

    def _voice(self, model, voice_path, prepare):
        key = (model, str(voice_path), os.path.getmtime(voice_path))
        if key not in self.voices:
            if len(self.voices) >= 24:
                self.voices.pop(next(iter(self.voices)))
            self.voices[key] = prepare()
        return self.voices[key]

    def _english(self, voice_path):
        model = self._cloner('en')

        def prepare():
            model.prepare_conditionals(str(voice_path), exaggeration=0, norm_loudness=True)
            return model._conds
        model._conds = self._voice('turbo', voice_path, prepare)
        return model, lambda text: model.generate(text=text, temperature=0.8, top_p=0.95, top_k=1000, repetition_penalty=1.2)

    def _other(self, voice_path, language):
        model = self._cloner(language)
        conds = self._voice('multilingual', voice_path, lambda: model.prepare_conditionals(str(voice_path), 24000))
        return model, lambda text: model.generate(text=text, conds=conds, lang_code=language)

    def speak(self, text, voice_path, seed, language='en'):
        """`text` spoken in the voice of the sample at `voice_path`, as (audio, rate).

        English uses Chatterbox Turbo, which is faster; other languages use multilingual Chatterbox.
        """
        import mlx.core as mx
        model, generate = self._english(voice_path) if language == 'en' else self._other(voice_path, language)
        words = len(text.split())
        audio = None
        # A short line sometimes rambles on past its text; a clip far longer than its words is retaken.
        for attempt in range(3):
            mx.random.seed(seed + attempt)
            audio = trim(self._generate(generate(text)), model.sample_rate)
            if len(audio) / model.sample_rate <= 2.5 + words * 0.9:
                break
        return audio, model.sample_rate
