"""Run matched Ming 0.5B 8-bit or me-read Chatterbox samples on Apple Silicon."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=('ming', 'chatterbox'), required=True)
    parser.add_argument('--model', type=Path)
    parser.add_argument('--models-dir', type=Path, default=Path.home() / '.local/share/me-read/models')
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.engine == 'ming' and args.model is None:
        parser.error('--model is required for Ming')
    if args.repeats < 1:
        parser.error('--repeats must be positive')

    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts import load

    args.out.mkdir(parents=True, exist_ok=True)
    cases = json.loads(Path(__file__).with_name('cases.json').read_text())
    environment = dict(python=sys.version, platform=platform.platform(),
                       hardware=subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip(),
                       mlx=importlib.metadata.version('mlx'), mlx_audio=importlib.metadata.version('mlx-audio'),
                       seed=7, uptime=subprocess.check_output(['uptime'], text=True).strip())
    engine = model = None
    load_seconds = None
    if args.engine == 'ming':
        started = time.perf_counter()
        model = load(str(args.model), strict=True)
        mx.eval(model.parameters())
        mx.synchronize()
        load_seconds = time.perf_counter() - started
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
        from meread.speech import Engine
        engine = Engine(args.models_dir)

    failures = 0
    for language in ('en', 'es'):
        log = args.out / f'{args.engine}-{language}.jsonl'
        if log.exists():
            raise FileExistsError(f'Choose a fresh output directory; {log} exists')

        def record(**fields):
            line = json.dumps(dict(engine=args.engine, language=language, **fields), ensure_ascii=False)
            with log.open('a') as stream:
                stream.write(line + '\n')
            print(line, flush=True)

        ref = args.references / f'{language}.wav'
        transcript = ref.with_suffix('.txt').read_text().strip()
        record(event='environment', **environment,
               model_id='mlx-community/Ming-3.0-tts-0.5B-8bit' if args.engine == 'ming' else
                        ('mlx-community/chatterbox-turbo-8bit' if language == 'en' else 'mlx-community/chatterbox-8bit'),
               reference_sha256=hashlib.sha256(ref.read_bytes()).hexdigest(), transcript=transcript)

        if args.engine == 'ming':
            settings = dict(ref_audio=str(ref), ref_text=transcript, lang_code=language, seed=7)
            record(event='load', seconds=load_seconds, shared_between_languages=True,
                   sample_rate=model.sample_rate, reference_preparation='included in each generation call')

            def generate(text):
                results = list(model.generate(text=text, **settings))
                parts = []
                for result in results:
                    mx.eval(result.audio)
                    parts.append(np.asarray(result.audio).reshape(-1))
                return np.concatenate(parts), model.sample_rate

        else:
            started = time.perf_counter()
            model = engine._cloner(language)
            mx.eval(model.parameters())
            mx.synchronize()
            record(event='load', seconds=time.perf_counter() - started, sample_rate=model.sample_rate)
            started = time.perf_counter()
            model, narrator = engine._english(ref) if language == 'en' else engine._other(ref, language)
            mx.synchronize()
            record(event='reference_preparation', seconds=time.perf_counter() - started)

            def generate(text):
                mx.random.seed(7)
                return engine._generate(narrator(text), model.sample_rate)

        def speak(case, repetition, warmup=False):
            nonlocal failures
            mx.random.seed(7)
            mx.reset_peak_memory()
            mx.synchronize()
            started = time.perf_counter()
            try:
                samples, rate = generate(case['text'])
                mx.synchronize()
                elapsed = time.perf_counter() - started
                if len(samples) < rate // 2 or not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-4:
                    raise RuntimeError('Invalid, empty, or silent output')
                name = f"{args.engine}-{case['id']}-r{repetition}.wav"
                sf.write(args.out / name, samples, rate, subtype='PCM_16')
                record(event='sample', case=case['id'], repetition=repetition, warmup=warmup, file=name,
                       text=case['text'], seconds=elapsed, audio_seconds=len(samples) / rate,
                       rtf=elapsed / (len(samples) / rate), peak_memory_bytes=mx.get_peak_memory(),
                       peak=float(np.max(np.abs(samples))), clipping_fraction=float(np.mean(np.abs(samples) >= 1)),
                       rms=float(np.sqrt(np.mean(samples ** 2))), status='ok')
            except Exception as error:
                failures += 1
                record(event='sample', case=case['id'], repetition=repetition, warmup=warmup,
                       status='error', error=str(error), traceback=traceback.format_exc())
                if warmup:
                    raise

        speak(dict(id=f'{language}-warmup', text='The door was open.' if language == 'en' else 'La puerta estaba abierta.'),
              0, warmup=True)
        for case in cases:
            if case['language'] != language:
                continue
            for repetition in range(1, (args.repeats if case['id'].endswith('narration') else 1) + 1):
                speak(case, repetition)
        record(event='complete', failures=failures)
        mx.clear_cache()
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
