"""Image service for me-read: FLUX.2 klein 4B behind a small HTTP API. It runs on the machine with the GPU."""
import argparse
import base64
import binascii
import io
import logging
import threading

import torch
import uvicorn
from diffusers import Flux2KleinPipeline, Flux2Transformer2DModel, GGUFQuantizationConfig
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

log = logging.getLogger('imager')
REPO = 'black-forest-labs/FLUX.2-klein-4B'
# The 4-bit transformer fits on a 16 GB card next to the text encoder, so nothing is swapped between images.
TRANSFORMER = 'https://huggingface.co/unsloth/FLUX.2-klein-4B-GGUF/blob/main/flux-2-klein-4b-Q4_K_M.gguf'
STEPS = 4


class Request(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    width: int = Field(512, ge=256, le=1024, multiple_of=16)
    height: int = Field(512, ge=256, le=1024, multiple_of=16)
    seed: int = 0
    references: list[str] = Field(default_factory=list, max_length=4)
    reference_px: int = Field(256, ge=128, le=1024, multiple_of=16)


class Painter:
    def __init__(self):
        self.pipe, self.error, self.lock = None, None, threading.Lock()

    def load(self):
        try:
            transformer = Flux2Transformer2DModel.from_single_file(
                TRANSFORMER, quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
                torch_dtype=torch.bfloat16, config=REPO, subfolder='transformer')
            self.pipe = Flux2KleinPipeline.from_pretrained(REPO, transformer=transformer, torch_dtype=torch.bfloat16).to('cuda')
            log.info('model ready')
        except Exception as e:
            log.exception('could not load the model')
            self.error = str(e)[:300]

    def paint(self, request, references):
        with self.lock:
            return self.pipe(prompt=request.prompt, image=references or None, width=request.width, height=request.height,
                             num_inference_steps=STEPS, guidance_scale=1.0,
                             generator=torch.Generator('cpu').manual_seed(request.seed)).images[0]


def decode(reference, size):
    try:
        image = Image.open(io.BytesIO(base64.b64decode(reference, validate=True)))
        return image.convert('RGB').resize((size, size), Image.LANCZOS)
    except (binascii.Error, UnidentifiedImageError, OSError) as e:
        raise HTTPException(422, 'a reference is not a base64 encoded image') from e


def create_app(painter):
    app = FastAPI(title='me-read imager')

    @app.get('/health')
    def health():
        return {'ready': painter.pipe is not None, 'model': REPO, 'error': painter.error}

    @app.post('/generate')
    def generate(request: Request):
        if painter.error:
            raise HTTPException(500, f'the model did not load: {painter.error}')
        if painter.pipe is None:
            raise HTTPException(503, 'the model is still loading')
        references = [decode(reference, request.reference_px) for reference in request.references]
        try:
            image = painter.paint(request, references)
        except Exception as e:
            log.exception('drawing failed')
            raise HTTPException(500, str(e)[:300]) from e
        out = io.BytesIO()
        image.save(out, format='PNG')
        return Response(out.getvalue(), media_type='image/png')

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # There is no login: anyone who can reach the port can use the GPU, so keep it on a network you trust.
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    painter = Painter()
    threading.Thread(target=painter.load, name='imager-load', daemon=True).start()
    uvicorn.run(create_app(painter), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
