"""Generate matched listening samples with Audio8-TTS-Realistic-Air. Same cases, seed and log format as run.py."""
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

SAMPLING = dict(max_new_tokens=1024, temperature=0.8, top_p=0.95, top_k=50, do_sample=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=('en', 'es'), required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--transcript', type=Path, required=True)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('cases.json'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--dtype', choices=('bfloat16', 'float16', 'float32'), default='bfloat16')
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--no-reference', action='store_true', help='Generate without voice cloning')
    args = parser.parse_args()

    import random
    import numpy as np
    import soundfile as sf
    import torch
    from transformers import AutoModel, AutoProcessor

    args.out.mkdir(parents=True, exist_ok=True)
    engine = 'audio8'
    log = args.out / f'{engine}-{args.language}.jsonl'

    def record(**fields):
        line = json.dumps(dict(engine=engine, language=args.language, **fields), ensure_ascii=False)
        with log.open('a') as stream:
            stream.write(line + '\n')
        print(line, flush=True)

    cases = [case for case in json.loads(args.cases.read_text()) if case['language'] == args.language]
    transcript = args.transcript.read_text().strip()
    record(event='environment', torch=torch.__version__, python=sys.version, platform=platform.platform(),
           gpu=torch.cuda.get_device_name(), free_vram=torch.cuda.mem_get_info()[0],
           reference_sha256=hashlib.sha256(args.reference.read_bytes()).hexdigest(),
           transcript=transcript, seed=7, dtype=args.dtype, cloning=not args.no_reference,
           packages={name: importlib.metadata.version(name) for name in ('transformers', 'soundfile', 'numpy')},
           uptime=subprocess.check_output(['uptime'], text=True).strip(), cpu_count=os.cpu_count())

    start = time.perf_counter()
    processor = AutoProcessor.from_pretrained(str(args.model), trust_remote_code=True)
    model = AutoModel.from_pretrained(str(args.model), trust_remote_code=True,
                                      dtype=getattr(torch, args.dtype)).eval().to('cuda')
    torch.cuda.synchronize()
    rate = model.config.codec_sample_rate
    record(event='load', seconds=time.perf_counter() - start, settings=SAMPLING, sample_rate=rate)

    def generate(text):
        fields = dict(text=[text], return_tensors='pt')
        if not args.no_reference:
            fields.update(reference_audio=[str(args.reference)], reference_text=[transcript])
        inputs = {name: value.to('cuda') for name, value in processor(**fields).items()}
        output = model.generate(**inputs, return_dict_in_generate=True, **SAMPLING)
        waveforms, lengths = model.decode_audio(output.codes)
        return waveforms[0, : int(lengths[0])]

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
                samples = generate(case['text']).detach().reshape(-1).float().cpu().numpy()
                torch.cuda.synchronize()
            elapsed = time.perf_counter() - start
            if len(samples) < rate // 2 or not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-4:
                raise RuntimeError('Invalid, empty, or silent audio')
            name = f"{engine}-{case['id']}-r{repetition}.wav"
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
