"""Read book paragraphs with the original PyTorch Ming in the narrator's saved voice. Run on the Radeon host.

usage: run-paragraph.py OUT_DIR REFERENCE.wav REFERENCE.txt CASES.json [SEEDS]
CASES.json is a list of {"id": ..., "text": ...}. One WAV per case and seed lands in OUT_DIR.
"""
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('MIOPEN_FIND_MODE', 'FAST')
os.environ.setdefault('GPU_MAX_HW_QUEUES', '1')

import json
import random

import numpy as np
import soundfile as sf
import torch

out, reference, transcript, cases = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), json.loads(Path(sys.argv[4]).read_text())
seeds = [int(s) for s in sys.argv[5].split(',')] if len(sys.argv) > 5 else [1, 2, 3]
out.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path('Ming-3.0-tts').resolve()))
from ming_tts import MingTTS

model = MingTTS('models/ming', text_normalization=False)
settings = dict(task='TTS', language_id='English', prompt_wav_path=reference, prompt_text=transcript.read_text().strip(),
                use_spk_emb=True, raw_text=True)


def read_wav(path, **kwargs):
    audio, rate = sf.read(path, dtype='float32', always_2d=True)
    return torch.from_numpy(audio.T.copy()), rate


with patch('torchaudio.load', read_wav):
    model.prepare_prompt(reference, True)
    for case in cases:
        for seed in seeds:
            random.seed(seed), np.random.seed(seed), torch.manual_seed(seed)
            start = time.perf_counter()
            with torch.inference_mode():
                samples = model.synthesize(text=case['text'], **settings).detach().reshape(-1).float().cpu().numpy()
            name = f"{case['id']}-s{seed}.wav"
            sf.write(out / name, samples, model.sample_rate, subtype='PCM_16')
            print(name, round(len(samples) / model.sample_rate, 2), 's audio in', round(time.perf_counter() - start, 1), 's', flush=True)
