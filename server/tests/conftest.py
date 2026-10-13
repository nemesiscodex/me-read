import io
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meread import images, scenes
from meread.app import create_app

PARAGRAPHS = [
    '<h2>Chapter One</h2>',
    '<p>The road was empty when Tom reached the gate.</p>',
    '<p>“Who goes there?” called a guard from the wall.</p>',
    '<p>“Only me,” said Tom. “Open up.”</p>',
    '<p>The gate creaked. Nobody spoke for a long time.</p>',
    '<p>“You are late,” said Anna, an old woman with a thin, high voice.</p>',
]
CHAPTER_TWO = ['<h2>Chapter Two</h2>', '<p>Morning came.</p>', '<p>“Tea?” asked Anna.</p>']


SPANISH = [
    '<h2>Capítulo uno</h2>',
    '<p>El camino estaba vacío cuando Tomás llegó a la puerta de la muralla, y no había nadie más que él.</p>',
    '<p>—¿Quién anda ahí? —gritó un guardia desde lo alto—. ¡Conteste!</p>',
    '<p>—Soy yo —dijo Tomás.</p>',
    '<p>La puerta crujió. Recordó las palabras de su madre: «Vuelve antes de que anochezca».</p>',
    '<p>—Llegas tarde.</p>',
]


def make_epub(chapters=(PARAGRAPHS, CHAPTER_TWO)):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml',
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        items = ''.join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
        refs = ''.join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        z.writestr('OEBPS/content.opf',
                   '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<metadata><dc:title>The Gate</dc:title><dc:creator>A. Writer</dc:creator></metadata>'
                   f'<manifest>{items}</manifest><spine>{refs}</spine></package>')
        for i, body in enumerate(chapters):
            z.writestr(f'OEBPS/c{i}.xhtml', f'<html><body>{"".join(body)}</body></html>')
    return data.getvalue()


class FakeEngine:
    """Stands in for the speech models: records what it was asked and returns short tones."""

    def __init__(self):
        self.designed, self.others, self.spoken, self.during_speak, self.languages = [], [], [], None, set()
        self.directed, self.direct_error = [], None

    def design(self, instruction, seed, language='en', others=()):
        self.designed.append(instruction)
        self.others.append(list(others))
        self.languages.add(language)
        return np.full(24000 * 7, 0.1, dtype='float32'), 24000

    def direct(self, voice_path, emotion, seed):
        self.directed.append((voice_path.name, emotion))
        if self.direct_error:
            raise self.direct_error
        return np.full(24000 * 7, 0.2, dtype='float32'), 24000

    def speak(self, text, voice_path, seed, language='en'):
        self.languages.add(language)
        self.spoken.append((text, voice_path.name, seed))
        if self.during_speak:
            self.during_speak()
        return np.full(12000, 0.1, dtype='float32'), 24000


class FakeTagger:
    """Answers like the LLM would for the test book, and keeps the prompts it saw.

    The system prompt of a call tells what is asked: who speaks (`answer`), which moments to draw (`scenes`), what
    a starred paragraph shows (`star`), or how one character looks (`look`).
    """

    def __init__(self):
        self.prompts, self.answer, self.error, self.cost = [], None, None, 0.002
        self.scene_prompts, self.scenes, self.scene_error = [], {'moments': [], 'ends': [], 'appearances': []}, None
        self.look_prompts, self.look, self.look_error = [], {'appearance': ''}, None
        self.star_prompts, self.star, self.star_error = [], {'moments': [], 'ends': [], 'appearances': []}, None

    def __call__(self, settings, prompt, cwd, system=None, schema=None):
        prompts, answer, error = {scenes.SYSTEM: (self.scene_prompts, self.scenes, self.scene_error),
                                  scenes.STAR_SYSTEM: (self.star_prompts, self.star, self.star_error),
                                  scenes.LOOK_SYSTEM: (self.look_prompts, self.look, self.look_error)}.get(
            system, (self.prompts, self.answer, self.error))
        prompts.append(prompt)
        if error:
            raise error
        return (answer(prompt) if callable(answer) else answer), self.cost


class FakeImager:
    """Stands in for the image service: records each request and returns a tiny PNG, or raises `error`."""

    def __init__(self):
        self.drawn, self.error, self.during = [], None, None

    def __call__(self, url, prompt, width, height, seed, references=()):
        if self.error:
            raise self.error
        self.drawn.append({'url': url, 'prompt': prompt, 'size': (width, height), 'seed': seed, 'references': list(references)})
        if self.during:
            self.during()
        return images.PNG + f'{seed}'.encode()


@pytest.fixture
def world(tmp_path):
    engine, ask, draw = FakeEngine(), FakeTagger(), FakeImager()
    app = create_app(tmp_path, engine=engine, ask=ask, draw=draw, run_workers=False)
    with TestClient(app) as client:
        book = client.post('/api/books', files={'file': ('gate.epub', make_epub(), 'application/epub+zip')}).json()

        class World:
            pass
        w = World()
        w.client, w.engine, w.ask, w.draw, w.store, w.pipeline, w.book = client, engine, ask, draw, app.state.store, app.state.pipeline, book
        w.events = []
        w.pipeline.publish = lambda book_id, event: w.events.append(event)

        def chapter(n=0):
            return client.get(f"/api/books/{book['id']}/chapters/{n}").json()

        def drain():
            while w.pipeline.tag_step() or w.pipeline.scene_step() or w.pipeline.speech_step() or w.pipeline.image_step():
                pass
        w.chapter, w.drain = chapter, drain
        yield w
