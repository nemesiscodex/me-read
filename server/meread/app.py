"""HTTP API and event stream for the reader."""
import asyncio
import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import epub, speech
from .pipeline import Pipeline
from .store import Store

Gender = Literal['male', 'female', 'unknown']
Age = Literal['child', 'teen', 'adult', 'elderly']
Pitch = Literal['low', 'medium', 'high']


class CharacterInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    gender: Gender = 'unknown'
    age: Age = 'adult'
    pitch: Pitch = 'medium'
    description: str = Field('', max_length=200)


class CharacterChanges(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    gender: Optional[Gender] = None
    age: Optional[Age] = None
    pitch: Optional[Pitch] = None
    description: Optional[str] = Field(None, max_length=200)


class SettingsChanges(BaseModel):
    provider: Optional[Literal['claude', 'codex']] = None
    claude_model: Optional[str] = Field(None, min_length=1, max_length=80)
    codex_model: Optional[str] = Field(None, min_length=1, max_length=80)
    tagging_paused: Optional[bool] = None


class Focus(BaseModel):
    first: int
    last: int
    current_first: int
    current_last: int


class Speaker(BaseModel):
    character_id: int


class Merge(BaseModel):
    into: int


class Hub:
    """Fans events out to the browsers watching a book. Workers publish from their own threads."""

    def __init__(self):
        self.loop = None
        self.listeners = {}

    def publish(self, book_id, event):
        if self.loop:
            self.loop.call_soon_threadsafe(self._deliver, book_id, event)

    def _deliver(self, book_id, event):
        for queue in self.listeners.get(book_id, ()):
            queue.put_nowait(event)

    async def stream(self, book_id, first):
        queue = asyncio.Queue()
        self.listeners.setdefault(book_id, set()).add(queue)
        try:
            yield f'data: {json.dumps(first)}\n\n'
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15)
                    yield f'data: {json.dumps(event)}\n\n'
                except asyncio.TimeoutError:
                    yield ': keep-alive\n\n'
        finally:
            self.listeners[book_id].discard(queue)


def create_app(home, engine=None, ask=None, web_dir=None, run_workers=True):
    store = Store(home)
    hub = Hub()
    engine = engine or speech.Engine(Path(home) / 'models/hub')
    pipeline = Pipeline(store, engine, publish=hub.publish, **({'ask': ask} if ask else {}))

    @asynccontextmanager
    async def lifespan(app):
        hub.loop = asyncio.get_running_loop()
        if run_workers:
            pipeline.start()
        yield
        pipeline.shutdown()

    app = FastAPI(title='me-read', lifespan=lifespan)
    app.state.store, app.state.pipeline = store, pipeline

    def found(row, what):
        if row is None:
            raise HTTPException(404, f'{what} not found')
        return row

    def character_of(character_id):
        return found(store.character(character_id), 'character')

    def segment_book(segment_id):
        return found(store.one('SELECT book_id FROM segment WHERE id = ?', (segment_id,)), 'segment')['book_id']

    def announce(book_id, where, args=()):
        hub.publish(book_id, {'type': 'characters', 'characters': store.characters(book_id)})
        segments = store.segments_where(where, args)
        if segments:
            hub.publish(book_id, {'type': 'segments', 'segments': segments})
        pipeline.nudge()

    @app.get('/api/books')
    def list_books():
        return store.books()

    @app.post('/api/books')
    def upload_book(file: UploadFile):
        with tempfile.NamedTemporaryFile(suffix='.epub') as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp.flush()
            try:
                parsed = epub.read(tmp.name)
            except epub.EpubError as e:
                raise HTTPException(400, str(e))
        book = store.book(store.add_book(parsed))
        return {k: book[k] for k in ('id', 'title', 'author', 'block_count', 'position_block', 'language')}

    @app.delete('/api/books/{book_id}', status_code=204)
    def delete_book(book_id: int):
        found(store.book(book_id), 'book')
        pipeline.forget(book_id)
        with store.lock:
            store.run('DELETE FROM segment WHERE book_id = ?', (book_id,))
            store.run('DELETE FROM book WHERE id = ?', (book_id,))
        shutil.rmtree(store.book_dir(book_id), ignore_errors=True)

    @app.get('/api/books/{book_id}')
    def get_book(book_id: int):
        return found(store.book(book_id), 'book')

    @app.get('/api/books/{book_id}/chapters/{chapter}')
    def get_chapter(book_id: int, chapter: int):
        return found(store.chapter_blocks(book_id, chapter), 'chapter')

    @app.post('/api/books/{book_id}/focus', status_code=204)
    def set_focus(book_id: int, focus: Focus):
        found(store.book(book_id), 'book')
        if focus.last < focus.first:
            raise HTTPException(422, 'last is before first')
        pipeline.set_focus(book_id, focus.first, focus.last, focus.current_first, focus.current_last)

    @app.get('/api/books/{book_id}/events')
    async def events(book_id: int):
        found(store.book(book_id), 'book')
        return StreamingResponse(hub.stream(book_id, {'type': 'activity', **pipeline.activity}),
                                 media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    @app.patch('/api/segments/{segment_id}')
    def set_speaker(segment_id: int, body: Speaker):
        book_id = segment_book(segment_id)
        if character_of(body.character_id)['book_id'] != book_id:
            raise HTTPException(422, 'that character belongs to another book')
        segment = store.set_speaker(segment_id, body.character_id)
        announce(book_id, 'id = ?', (segment_id,))
        return segment

    @app.post('/api/segments/{segment_id}/regenerate')
    def regenerate_segment(segment_id: int):
        book_id = segment_book(segment_id)
        store.invalidate_audio('id = ?', (segment_id,))
        announce(book_id, 'id = ?', (segment_id,))
        return store.segment(segment_id)

    @app.get('/api/segments/{segment_id}/audio')
    def segment_audio(segment_id: int):
        path = store.audio_path(segment_book(segment_id), segment_id)
        if store.segment(segment_id)['audio_state'] != 'ready' or not path.exists():
            raise HTTPException(404, 'no audio for this segment yet')
        return FileResponse(path, media_type='audio/flac', headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    @app.post('/api/blocks/{block_id}/retag', status_code=204)
    def retag_block(block_id: int):
        block = found(store.one('SELECT book_id FROM block WHERE id = ?', (block_id,)), 'paragraph')
        store.retag_block(block_id)
        hub.publish(block['book_id'], {'type': 'blocks', 'blocks': [{'id': block_id, 'tag_state': 'pending'}]})
        announce(block['book_id'], 'block_id = ?', (block_id,))

    @app.post('/api/books/{book_id}/characters')
    def create_character(book_id: int, body: CharacterInput):
        found(store.book(book_id), 'book')
        character = store.add_character(book_id, body.name, 'named', body.gender, body.age, body.pitch, body.description)
        announce(book_id, '0')
        return character

    @app.patch('/api/characters/{character_id}')
    def update_character(character_id: int, body: CharacterChanges):
        book_id = character_of(character_id)['book_id']
        character = store.update_character(character_id, body.model_dump(exclude_none=True))
        announce(book_id, 'character_id = ?', (character_id,))
        return character

    @app.post('/api/characters/{character_id}/voice/draft')
    def request_draft(character_id: int):
        book_id = character_of(character_id)['book_id']
        store.request_draft(character_id)
        announce(book_id, '0')
        return store.character(character_id)

    @app.delete('/api/characters/{character_id}/voice/draft')
    def discard_draft(character_id: int):
        book_id = character_of(character_id)['book_id']
        store.discard_draft(character_id)
        announce(book_id, '0')
        return store.character(character_id)

    @app.post('/api/characters/{character_id}/voice/draft/approve')
    def approve_draft(character_id: int):
        book_id = character_of(character_id)['book_id']
        if not store.approve_draft(character_id):
            raise HTTPException(409, 'there is no candidate voice to approve')
        announce(book_id, 'character_id = ?', (character_id,))
        return store.character(character_id)

    @app.get('/api/characters/{character_id}/voice/draft')
    def draft_voice(character_id: int):
        character = character_of(character_id)
        path = store.draft_path(character)
        if character['draft_state'] != 'ready' or not path.exists():
            raise HTTPException(404, 'this character has no candidate voice')
        return FileResponse(path, media_type='audio/wav', headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    @app.post('/api/characters/{character_id}/voice')
    def upload_voice(character_id: int, file: UploadFile):
        character = character_of(character_id)
        with tempfile.NamedTemporaryFile(suffix=Path(file.filename or 'sample').suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp.flush()
            try:
                audio, rate = speech.read_sample(tmp.name)
            except ValueError as e:
                raise HTTPException(400, str(e))
        import soundfile as sf
        path = store.voice_path(character)
        path.parent.mkdir(parents=True, exist_ok=True)
        with store.lock:
            store.reset_voice(character_id)
            sf.write(path, audio, rate, format='WAV')
            store.run("UPDATE character SET voice_state = 'ready' WHERE id = ?", (character_id,))
        announce(character['book_id'], 'character_id = ?', (character_id,))
        return store.character(character_id)

    @app.get('/api/characters/{character_id}/voice')
    def character_voice(character_id: int):
        character = character_of(character_id)
        path = store.voice_path(character)
        if character['voice_state'] != 'ready' or not path.exists():
            raise HTTPException(404, 'this character has no voice yet')
        return FileResponse(path, media_type='audio/wav', headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    @app.post('/api/characters/{character_id}/merge', status_code=204)
    def merge_character(character_id: int, body: Merge):
        character, into = character_of(character_id), character_of(body.into)
        if character['role'] == 'narrator':
            raise HTTPException(422, 'the narrator cannot be merged away')
        if into['book_id'] != character['book_id'] or into['id'] == character_id:
            raise HTTPException(422, 'pick another character of the same book')
        store.merge_character(character_id, body.into)
        store.voice_path(character).unlink(missing_ok=True)
        store.draft_path(character).unlink(missing_ok=True)
        announce(character['book_id'], 'character_id = ?', (body.into,))

    @app.get('/api/settings')
    def get_settings():
        return store.settings()

    @app.put('/api/settings')
    def update_settings(body: SettingsChanges):
        settings = store.update_settings(body.model_dump(exclude_none=True))
        pipeline.nudge()
        return settings

    @app.get('/api/usage')
    def get_usage():
        return store.usage()

    if web_dir and Path(web_dir).is_dir():
        app.mount('/', StaticFiles(directory=web_dir, html=True), name='web')
    return app
