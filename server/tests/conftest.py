import io
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

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
        self.designed, self.spoken, self.during_speak, self.languages = [], [], None, set()

    def design(self, instruction, seed, language='en'):
        self.designed.append(instruction)
        self.languages.add(language)
        return np.full(24000 * 7, 0.1, dtype='float32'), 24000

    def speak(self, text, voice_path, seed, language='en'):
        self.languages.add(language)
        self.spoken.append((text, voice_path.name, seed))
        if self.during_speak:
            self.during_speak()
        return np.full(12000, 0.1, dtype='float32'), 24000


class FakeTagger:
    """Answers like the LLM would for the test book, and keeps the prompts it saw."""

    def __init__(self):
        self.prompts, self.answer, self.error, self.cost = [], None, None, 0.002

    def __call__(self, settings, prompt, cwd):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return (self.answer(prompt) if callable(self.answer) else self.answer), self.cost


@pytest.fixture
def world(tmp_path):
    engine, ask = FakeEngine(), FakeTagger()
    app = create_app(tmp_path, engine=engine, ask=ask, run_workers=False)
    with TestClient(app) as client:
        book = client.post('/api/books', files={'file': ('gate.epub', make_epub(), 'application/epub+zip')}).json()

        class World:
            pass
        w = World()
        w.client, w.engine, w.ask, w.store, w.pipeline, w.book = client, engine, ask, app.state.store, app.state.pipeline, book
        w.events = []
        w.pipeline.publish = lambda book_id, event: w.events.append(event)

        def chapter(n=0):
            return client.get(f"/api/books/{book['id']}/chapters/{n}").json()

        def drain():
            while w.pipeline.tag_step() or w.pipeline.speech_step():
                pass
        w.chapter, w.drain = chapter, drain
        yield w
