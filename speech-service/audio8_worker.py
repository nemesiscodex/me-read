"""Audio8 TTS in its own process, for the speech service. Run it with the Python of the Audio8 environment.

It answers the part of CrispASR's HTTP API that the service's Child uses: GET /health and POST /v1/audio/speech
with a named voice from --voice-dir. Audio8 needs transformers below 5, which the service's own environment does not have.
"""
import argparse
import io
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.setdefault('MIOPEN_FIND_MODE', 'FAST')
os.environ.setdefault('GPU_MAX_HW_QUEUES', '1')

log = logging.getLogger('audio8')
DISCLAIMER = 'X-Crispasr-Spoken-Disclaimer'
SAMPLING = dict(max_new_tokens=1024, temperature=0.8, top_p=0.95, top_k=50, do_sample=True)


class Audio8:
    def __init__(self, folder, dtype):
        import torch
        from transformers import AutoModel, AutoProcessor
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(str(folder), trust_remote_code=True)
        self.model = AutoModel.from_pretrained(str(folder), trust_remote_code=True, dtype=getattr(torch, dtype)).eval().to('cuda')
        self.rate = self.model.config.codec_sample_rate

    def speak(self, text, voice, transcript, seed):
        import numpy as np
        import soundfile as sf
        torch = self.torch
        inputs = self.processor(text=[text], reference_audio=[str(voice)], reference_text=[transcript], return_tensors='pt')
        inputs = {name: value.to('cuda') for name, value in inputs.items()}
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        with torch.inference_mode():
            output = self.model.generate(**inputs, return_dict_in_generate=True, **SAMPLING)
            waveforms, lengths = self.model.decode_audio(output.codes)
        samples = waveforms[0, : int(lengths[0])].float().cpu().numpy()
        if not len(samples) or not np.isfinite(samples).all() or not samples.any():
            raise RuntimeError('Audio8 returned empty, silent or non-finite audio')
        out = io.BytesIO()
        sf.write(out, samples, self.rate, format='WAV', subtype='PCM_16')
        return out.getvalue()


def handler(audio8, voices):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            log.debug(format, *args)

        def reply(self, status, body, content_type='application/json', headers=()):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def problem(self, status, code, message):
            self.reply(status, json.dumps({'error': {'code': code, 'message': message}}).encode())

        def do_GET(self):
            if self.path == '/health':
                return self.reply(200, b'{"status":"ok"}')
            self.problem(404, 'not_found', self.path)

        def do_POST(self):
            if self.path != '/v1/audio/speech':
                return self.problem(404, 'not_found', self.path)
            try:
                request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                voice = voices / f"{request['voice']}.wav"
                if not voice.is_file():
                    return self.problem(500, 'synthesis_failed', f"the voice {request['voice']} is not uploaded")
                transcript = request.get('ref_text')
                if not transcript:
                    return self.problem(400, 'ref_text_required', 'Audio8 needs the transcript of its voice')
                wav = audio8.speak(request['input'], voice, transcript, int(request.get('seed', 0)))
            except (KeyError, ValueError) as e:
                return self.problem(400, 'invalid_request', str(e)[:200])
            except Exception as e:
                log.exception('synthesis failed')
                return self.problem(500, 'synthesis_failed', str(e)[:300])
            self.reply(200, wav, 'audio/wav', [(DISCLAIMER, 'skipped')])
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True, help='the Audio8 checkpoint folder')
    parser.add_argument('--voice-dir', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    # float16 runs about 2.6 times faster than bfloat16 on the RX 6900 XT, whose bfloat16 is not accelerated.
    parser.add_argument('--dtype', choices=('float16', 'bfloat16', 'float32'), default='float16')
    args, _ = parser.parse_known_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s', stream=sys.stderr)
    audio8 = Audio8(args.model, args.dtype)
    HTTPServer((args.host, args.port), handler(audio8, args.voice_dir)).serve_forever()


if __name__ == '__main__':
    main()
