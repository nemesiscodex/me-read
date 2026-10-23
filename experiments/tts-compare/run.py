"""Generate matched listening samples. Run each engine in its own Python environment."""
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

os.environ.setdefault('MIOPEN_FIND_MODE', 'FAST')
os.environ.setdefault('GPU_MAX_HW_QUEUES', '1')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=('ming', 'chatterbox'), required=True)
    parser.add_argument('--language', choices=('en', 'es'), required=True)
    parser.add_argument('--ming-source', type=Path)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--transcript', type=Path, required=True)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('cases.json'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=5,
                        help='Measured narration repetitions, after one warmup (default: 5)')
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be at least 1')

    import random
    import numpy as np
    import soundfile as sf
    import torch

    args.out.mkdir(parents=True, exist_ok=True)
    log = args.out / f'{args.engine}-{args.language}.jsonl'

    def record(**fields):
        line = json.dumps(dict(engine=args.engine, language=args.language, **fields), ensure_ascii=False)
        with log.open('a') as stream:
            stream.write(line + '\n')
        print(line, flush=True)

    cases = [case for case in json.loads(args.cases.read_text()) if case['language'] == args.language]
    record(event='environment', torch=torch.__version__, python=sys.version, platform=platform.platform(),
           gpu=torch.cuda.get_device_name(), free_vram=torch.cuda.mem_get_info()[0],
           reference_sha256=hashlib.sha256(args.reference.read_bytes()).hexdigest(),
           transcript=args.transcript.read_text().strip(), seed=7,
           packages={name: importlib.metadata.version(name) for name in
                     ('transformers', 'torchaudio', 'numpy', 'soundfile')},
           uptime=subprocess.check_output(['uptime'], text=True).strip(),
           cpu_count=os.cpu_count())

    start = time.perf_counter()
    if args.engine == 'ming':
        if args.ming_source is None:
            parser.error('--ming-source is required for Ming')
        sys.path.insert(0, str(args.ming_source.resolve()))
        from ming_tts import MingTTS
        model = MingTTS(str(args.model), text_normalization=False)
        settings = dict(task='TTS' if args.language == 'en' else 'MULTILINGUAL',
                        language_id='English' if args.language == 'en' else 'Spanish',
                        prompt_wav_path=str(args.reference), prompt_text=args.transcript.read_text().strip(),
                        use_spk_emb=True, raw_text=True)

        def generate(text):
            return model.synthesize(text=text, **settings)

        rate = model.sample_rate
    else:
        if args.language == 'en':
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            model = ChatterboxTurboTTS.from_local(args.model, device='cuda')
            settings = dict(temperature=0.8, top_p=0.95, top_k=1000, repetition_penalty=1.2)
        else:
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            model = ChatterboxMultilingualTTS.from_local(args.model, device='cuda', t3_model='v3')
            settings = dict(language_id='es')

        def generate(text):
            return model.generate(text, **settings)

        rate = model.sr

    torch.cuda.synchronize()
    record(event='load', seconds=time.perf_counter() - start, settings=settings, sample_rate=rate)
    random.seed(7)
    np.random.seed(7)
    torch.manual_seed(7)
    start = time.perf_counter()
    if args.engine == 'ming':
        # ROCm needs torch 2.9 here; restore the soundfile WAV reader used by torch 2.6.
        # The new TorchCodec backend does not ship a working build for this ROCm runtime.
        from unittest.mock import patch

        def read_wav(path, **kwargs):
            audio, sample_rate = sf.read(path, dtype='float32', always_2d=True)
            return torch.from_numpy(audio.T.copy()), sample_rate

        with patch('torchaudio.load', read_wav):
            model.prepare_prompt(str(args.reference), True)
    elif args.language == 'en':
        model.prepare_conditionals(str(args.reference), exaggeration=0, norm_loudness=True)
    else:
        model.prepare_conditionals(str(args.reference))
    torch.cuda.synchronize()
    record(event='reference_preparation', seconds=time.perf_counter() - start)

    failures = 0

    def speak(case, repetition, warmup=False):
        nonlocal failures
        random.seed(7)
        np.random.seed(7)
        torch.manual_seed(7)
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        start = time.perf_counter()
        try:
            with torch.inference_mode():
                wav = generate(case['text'])
                samples = wav.detach().reshape(-1).float().cpu().numpy()
                torch.cuda.synchronize()
            elapsed = time.perf_counter() - start
            if len(samples) < rate // 2 or not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-4:
                raise RuntimeError('Invalid, empty, or silent audio')
            name = f"{args.engine}-{case['id']}-r{repetition}.wav"
            sf.write(args.out / name, samples, rate, subtype='PCM_16')
            record(event='sample', case=case['id'], repetition=repetition, warmup=warmup, file=name,
                   text=case['text'], seconds=elapsed, audio_seconds=len(samples) / rate,
                   rtf=elapsed / (len(samples) / rate), peak_vram=torch.cuda.max_memory_allocated(),
                   peak=float(np.max(np.abs(samples))), clipping_fraction=float(np.mean(np.abs(samples) >= 1)),
                   rms=float(np.sqrt(np.mean(samples ** 2))), status='ok')
        except Exception as error:
            failures += 1
            record(event='sample', case=case['id'], repetition=repetition, warmup=warmup,
                   status='error', seconds=time.perf_counter() - start, error=str(error),
                   traceback=traceback.format_exc())
            if warmup:
                raise

    warmup_text = 'The door was open.' if args.language == 'en' else 'La puerta estaba abierta.'
    speak(dict(id=f'{args.language}-warmup', text=warmup_text), 0, warmup=True)
    for case in cases:
        for repetition in range(1, (args.repeats if case['id'].endswith('narration') else 1) + 1):
            speak(case, repetition)
    record(event='complete', failures=failures)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
