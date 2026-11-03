"""The original Qwen3-TTS 12Hz 0.6B Base in its own process, for the speech service. Run it with the Python of the Qwen environment.

It answers the part of CrispASR's HTTP API that the service's Child uses: GET /health and POST /v1/audio/speech
with a named voice from --voice-dir. qwen-tts needs transformers 4.57.3, which the service's own environment does not have.
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

log = logging.getLogger('qwen')
DISCLAIMER = 'X-Crispasr-Spoken-Disclaimer'
LANGUAGES = {'en': 'English', 'es': 'Spanish'}


FRAMES_PER_SECOND = 12.5


def frame_limit(text):
    """The most frames a line may take. The model's own limit is 8192, nearly eleven minutes, and a line that does not
    stop would hold the GPU that long. me-read retakes a clip longer than 2.5 seconds plus 0.9 per word, and this limit
    is always longer than that, so a clip cut off here is retaken."""
    return int(FRAMES_PER_SECOND * (3.5 + len(text.split())))


class Qwen:
    def __init__(self, folder):
        import torch
        from qwen_tts import Qwen3TTSModel
        self.torch = torch
        self.model = Qwen3TTSModel.from_pretrained(str(folder), device_map='cuda:0', dtype=torch.float16,
                                                   attn_implementation='sdpa')
        self.in_float32(self.model.model.talker.code_predictor.model)
        self.prompts = {}

    def in_float32(self, predictor):
        """Run the code predictor's layers in float32 and hand float16 back to the talker.

        In float16 its MLP product act(gate) * up passes 65504 and becomes inf. Sampling from that crashes the GPU with
        a hardware exception, which kills the process. The main model peaks near 1100 and stays in float16. The
        embeddings stay float16 as well, because the talker adds them to its own hidden states."""
        torch = self.torch
        for layer in predictor.layers:
            layer.float()
        predictor.norm.float()
        original = predictor.forward

        def forward(*args, inputs_embeds=None, **kwargs):
            result = original(*args, inputs_embeds=None if inputs_embeds is None else inputs_embeds.float(), **kwargs)
            result.last_hidden_state = result.last_hidden_state.to(torch.float16)
            return result
        predictor.forward = forward

    def speak(self, text, language, voice, transcript, seed):
        import numpy as np
        import soundfile as sf
        torch = self.torch
        key = (str(voice), voice.stat().st_mtime_ns, transcript)
        if key not in self.prompts:
            if len(self.prompts) >= 24:
                self.prompts.pop(next(iter(self.prompts)))
            self.prompts[key] = self.model.create_voice_clone_prompt(ref_audio=str(voice), ref_text=transcript)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        with torch.inference_mode():
            wavs, rate = self.model.generate_voice_clone(text=text, language=LANGUAGES.get(language, 'Auto'),
                                                         voice_clone_prompt=self.prompts[key],
                                                         max_new_tokens=frame_limit(text))
        samples = np.asarray(wavs[0], dtype='float32')
        if not len(samples) or not np.isfinite(samples).all() or not samples.any():
            raise RuntimeError('Qwen3-TTS returned empty, silent or non-finite audio')
        out = io.BytesIO()
        sf.write(out, samples, rate, format='WAV', subtype='PCM_16')
        return out.getvalue()


def handler(qwen, voices):
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
                    return self.problem(400, 'ref_text_required', 'Qwen3-TTS needs the transcript of its voice')
                wav = qwen.speak(request['input'], request.get('language'), voice, transcript, int(request.get('seed', 0)))
            except (KeyError, ValueError) as e:
                return self.problem(400, 'invalid_request', str(e)[:200])
            except Exception as e:
                log.exception('synthesis failed')
                return self.problem(500, 'synthesis_failed', str(e)[:300])
            self.reply(200, wav, 'audio/wav', [(DISCLAIMER, 'skipped')])
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True, help='the Qwen3-TTS Base checkpoint folder')
    parser.add_argument('--voice-dir', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    args, _ = parser.parse_known_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s', stream=sys.stderr)
    qwen = Qwen(args.model)
    HTTPServer((args.host, args.port), handler(qwen, args.voice_dir)).serve_forever()


if __name__ == '__main__':
    main()
