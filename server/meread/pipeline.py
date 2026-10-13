"""Background work for the pages the reader is on: tag spoken lines, design voices, generate clips, and when an
image service is set, pick the moments to illustrate and draw them.

Four workers each repeat one step: look in the database for the most useful next job inside the reader's
focus, do it, record the result. Nothing is queued in memory, so a restart or a correction just changes what
the next lookup finds.
"""
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import images, scenes, speech, tagger
from .segment import speakable
from .store import illustrating

log = logging.getLogger('meread')
TAG_BATCH = 20
SCENE_BATCH = 30
# Seconds before asking again after the scene call failed, and after the image or speech service did not answer.
SCENE_RETRY = 60
IMAGE_RETRY = 15
SPEECH_RETRY = 15
CONTEXT_BLOCKS = 8
MAX_FOCUS = 400
SILENCE = 0.6, 24000
WORKERS = 'tagger', 'scenes', 'speech', 'images'
# What a drawing is claimed and landed through: table, state column, revision column, error column.
PORTRAIT = 'character', 'portrait_state', 'portrait_rev', 'portrait_error'
MOMENT = 'moment', 'image_state', 'image_rev', 'error'


class Pipeline:
    def __init__(self, store, engine, ask=tagger.ask, draw=images.generate, publish=lambda book_id, event: None):
        self.store, self.engine, self.ask, self.draw, self.publish = store, engine, ask, draw, publish
        self.focus = None
        self.activity = dict.fromkeys(WORKERS)
        self.idle = dict.fromkeys(WORKERS)
        self.wake = {name: threading.Event() for name in WORKERS}
        self.retry_at = {}
        self.stop = threading.Event()
        self.threads = []
        self.llm_cwd = store.home / 'llm'
        self.llm_cwd.mkdir(exist_ok=True)

    # Control

    def set_focus(self, book_id, first, last, current_first, current_last, position=None):
        first, last = max(0, first), min(last, first + MAX_FOCUS)
        self.focus = {'book_id': book_id, 'first': first, 'last': last,
                      'current_first': min(max(current_first, first), last), 'current_last': current_last}
        # The line at the top of the page. Without one, the first paragraph on the page stands in.
        block, seq = position or (self.focus['current_first'], 0)
        self.store.run('UPDATE book SET position_block = ?, position_seq = ? WHERE id = ?', (block, seq, book_id))
        self.nudge()

    def forget(self, book_id):
        if self.focus and self.focus['book_id'] == book_id:
            self.focus = None

    def nudge(self):
        for event in self.wake.values():
            event.set()

    def start(self):
        for name, step in zip(WORKERS, (self.tag_step, self.scene_step, self.speech_step, self.image_step)):
            thread = threading.Thread(target=self._loop, args=(name, step), name=f'meread-{name}', daemon=True)
            thread.start()
            self.threads.append(thread)

    def shutdown(self):
        self.stop.set()
        self.nudge()

    def _loop(self, name, step):
        while not self.stop.is_set():
            try:
                busy = step()
            except Exception:
                log.exception('%s step failed', name)
                busy = False
            if not busy:
                self._say(name, self.idle[name])
                self.wake[name].wait(1.0)
                self.wake[name].clear()

    def _say(self, worker, message):
        if self.activity[worker] != message:
            self.activity[worker] = message
            if self.focus:
                self.publish(self.focus['book_id'], {'type': 'activity', **self.activity})

    def _publish_segments(self, book_id, where, args=()):
        segments = self.store.segments_where(where, args)
        if segments:
            self.publish(book_id, {'type': 'segments', 'segments': segments})

    def _publish_cast(self, book_id):
        self.publish(book_id, {'type': 'characters', 'characters': self.store.characters(book_id)})

    def _publish_moments(self, book_id, where, args=()):
        moments = self.store.moments_where(where, args)
        if moments:
            self.publish(book_id, {'type': 'moments', 'moments': moments})

    # Tagging

    def tag_step(self):
        """Attribute the next run of untagged paragraphs in focus. Returns False when there is nothing to tag."""
        focus = self.focus
        settings = self.store.settings()
        self.idle['tagger'] = 'Tagging is paused; the narrator reads untagged lines' if settings['tagging_paused'] else (
            self.idle['tagger'] if (self.idle['tagger'] or '').startswith('Tagging failed') else None)
        if not focus or settings['tagging_paused']:
            return False
        book_id = focus['book_id']
        pending = self.store.all(
            "SELECT id, idx FROM block WHERE book_id = ? AND idx BETWEEN ? AND ? AND tag_state = 'pending' "
            'ORDER BY idx < ?, idx LIMIT ?',
            (book_id, focus['first'], focus['last'], focus['current_first'], TAG_BATCH))
        if not pending:
            return False
        start = pending[0]['idx']
        batch = [b for b in pending if b['idx'] >= start]
        ids = [b['id'] for b in batch]
        marks = ','.join('?' * len(ids))
        self.store.run(f"UPDATE block SET tag_state = 'tagging' WHERE id IN ({marks})", ids)
        self.publish(book_id, {'type': 'blocks', 'blocks': [{'id': i, 'tag_state': 'tagging'} for i in ids]})
        book = self.store.one('SELECT title, author, language FROM book WHERE id = ?', (book_id,))
        self._say('tagger', f"Asking {settings['provider']} who speaks in {len(batch)} paragraphs")
        prompt, markers = tagger.build_prompt(
            book, self.store.characters(book_id),
            self.store.blocks(book_id, start - CONTEXT_BLOCKS, start - 1),
            self.store.blocks(book_id, start, batch[-1]['idx']))
        problem = None
        try:
            answer, cost = self.ask(settings, prompt, self.llm_cwd)
            self.store.record_usage(settings['provider'], settings[f"{settings['provider']}_model"], len(batch), cost)
            tagger.apply(self.store, book_id, markers, answer)
        except tagger.TaggerError as e:
            problem = str(e)
            log.warning('tagging failed: %s', e)
        # A line the tagger left without a speaker is read by the narrator, and its paragraph is flagged.
        narrator = self.store.narrator(book_id)['id']
        states = []
        with self.store.lock:
            for block_id in ids:
                # A paragraph sent back for tagging while this ran keeps its place in the queue.
                if self.store.one('SELECT tag_state FROM block WHERE id = ?', (block_id,))['tag_state'] != 'tagging':
                    continue
                missed = self.store.run('UPDATE segment SET character_id = ? WHERE block_id = ? AND character_id IS NULL',
                                        (narrator, block_id)).rowcount
                state = 'failed' if missed else 'tagged'
                self.store.run('UPDATE block SET tag_state = ? WHERE id = ?', (state, block_id))
                states.append({'id': block_id, 'tag_state': state})
        self._publish_cast(book_id)
        self._publish_segments(book_id, f'block_id IN ({marks})', ids)
        self.publish(book_id, {'type': 'blocks', 'blocks': states})
        self.idle['tagger'] = f'Tagging failed: {problem}' if problem else None
        self.wake['speech'].set()
        self.wake['scenes'].set()
        return True

    # Moments to illustrate

    def scene_step(self):
        """Look for moments in the next run of unscanned paragraphs on the current and next page. The page before is
        only ever context. Returns False when there is nothing to scan."""
        focus = self.focus
        settings = self.store.settings()
        # The pause switch promises no Claude or Codex calls at all.
        if not focus or not illustrating(settings) or settings['tagging_paused']:
            self.idle['scenes'] = None
            return False
        if time.monotonic() < self.retry_at.get('scenes', 0):
            return False
        book_id = focus['book_id']
        pending = self.store.all(
            "SELECT id, idx, tag_state FROM block WHERE book_id = ? AND idx BETWEEN ? AND ? AND scene_state = 'pending' "
            'ORDER BY idx LIMIT ?', (book_id, focus['current_first'], focus['last'], SCENE_BATCH))
        batch = [b for n, b in enumerate(pending) if b['idx'] == pending[0]['idx'] + n]
        # The cast of a paragraph is only known once its lines are tagged, and scanning around the gaps would
        # spend a call on every few paragraphs.
        if not batch or any(b['tag_state'] in ('pending', 'tagging') for b in batch):
            return False
        start, ids = batch[0]['idx'], [b['id'] for b in batch]
        marks = ','.join('?' * len(ids))
        self.store.run(f"UPDATE block SET scene_state = 'scanning' WHERE id IN ({marks})", ids)
        book = self.store.one('SELECT title, author, language FROM book WHERE id = ?', (book_id,))
        self._say('scenes', f"Asking {settings['provider']} for moments to draw in {len(batch)} paragraphs")
        last = self.store.one('SELECT kind, summary, scene, block_idx FROM moment WHERE book_id = ? AND block_idx < ? '
                              'ORDER BY block_idx DESC LIMIT 1', (book_id, start))
        prompt, markers = scenes.build_prompt(
            book, self.store.characters(book_id), last,
            self.store.blocks(book_id, start - CONTEXT_BLOCKS, start - 1),
            self.store.blocks(book_id, start, batch[-1]['idx']))
        try:
            answer, cost = self.ask(settings, prompt, self.llm_cwd, system=scenes.SYSTEM, schema=scenes.SCHEMA)
            self.store.record_usage(settings['provider'], settings[f"{settings['provider']}_model"], len(batch), cost)
            created = scenes.apply(self.store, book_id, markers, answer)
        except tagger.TaggerError as e:
            log.warning('looking for moments failed: %s', e)
            self.store.run(f"UPDATE block SET scene_state = 'pending' WHERE scene_state = 'scanning' AND id IN ({marks})", ids)
            self.idle['scenes'] = f'Looking for moments to draw failed: {e}'
            self.retry_at['scenes'] = time.monotonic() + SCENE_RETRY
            return False
        self.store.run(f"UPDATE block SET scene_state = 'done' WHERE scene_state = 'scanning' AND id IN ({marks})", ids)
        self.idle['scenes'] = None
        self._publish_cast(book_id)
        self._publish_moments(book_id, f"m.id IN ({','.join('?' * len(created))})", created)
        self.wake['images'].set()
        return True

    # Voices and clips

    def speech_step(self):
        """Generate the next missing clip in focus, designing its voice or making its take first when needed."""
        draft = self.store.one("SELECT id FROM character WHERE draft_state = 'queued' ORDER BY id LIMIT 1")
        if draft:
            self.design_draft(self.store.character(draft['id']))
            return True
        queued = self.store.one("SELECT character_id, emotion FROM voice_take WHERE state = 'queued' "
                                'ORDER BY character_id, emotion LIMIT 1')
        if queued:
            self.make_take(self.store.character(queued['character_id']),
                           self.store.take(queued['character_id'], queued['emotion']))
            return True
        focus = self.focus
        if not focus:
            return False
        if time.monotonic() < self.retry_at.get('speech', 0):
            return False
        book_id = focus['book_id']
        provider = getattr(self.engine, 'provider', None)
        try:
            if provider is not None:
                provider.validate_language(self._language(book_id))
        except ValueError as e:
            changed = self.store.run("UPDATE segment SET audio_state = 'failed', error = ? WHERE book_id = ? "
                                     "AND block_idx BETWEEN ? AND ? AND audio_state = 'none'",
                                     (str(e), book_id, focus['first'], focus['last'])).rowcount
            if changed:
                self._publish_segments(book_id, 'book_id = ? AND block_idx BETWEEN ? AND ?',
                                       (book_id, focus['first'], focus['last']))
            self.idle['speech'] = str(e)
            return bool(changed)
        self.idle['speech'] = None
        # While tagging is paused the narrator reads lines that have no speaker yet.
        paused = self.store.settings()['tagging_paused']
        seg = self.store.one(
            "SELECT s.id, s.text, s.audio_rev, s.character_id, s.emotion FROM segment s "
            "WHERE s.book_id = ? AND s.block_idx BETWEEN ? AND ? AND s.audio_state = 'none' "
            'AND (s.character_id IS NOT NULL OR ?) ORDER BY s.block_idx < ?, s.block_idx, s.seq LIMIT 1',
            (book_id, focus['first'], focus['last'], paused, focus['current_first']))
        if seg:
            character = self.store.character(seg['character_id']) if seg['character_id'] else self.store.narrator(book_id)
            if character['voice_state'] == 'failed':
                self.store.run("UPDATE segment SET audio_state = 'failed', error = ? WHERE id = ? AND audio_rev = ?",
                               (f"{character['name']} has no voice: {character['error']}", seg['id'], seg['audio_rev']))
                self._publish_segments(book_id, 'id = ?', (seg['id'],))
            elif character['voice_state'] != 'ready':
                self.design_voice(character)
            else:
                # Breeze directs emotions in English only.
                emotion = seg['emotion'] if self._language(book_id) == 'en' else 'neutral'
                take = self.store.take(character['id'], emotion) if emotion != 'neutral' else None
                if take and take['state'] in ('none', 'queued'):
                    self.make_take(character, take)
                else:
                    # A line whose take could not be made is read in the character's own voice.
                    self.generate(book_id, seg, character, emotion if take and take['state'] == 'ready' else 'neutral')
            return True
        waiting = self.store.one("SELECT id FROM character WHERE book_id = ? AND voice_state = 'none' LIMIT 1", (book_id,))
        if waiting:
            self.design_voice(self.store.character(waiting['id']))
            return True
        return False

    def _language(self, book_id):
        return self.store.one('SELECT language FROM book WHERE id = ?', (book_id,))['language']

    def _land(self, write, target, claim):
        """Write a file next to `target` and move it into place only if `claim` still holds in the database."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=target.suffix)
        os.close(fd)
        try:
            write(tmp)
            with self.store.lock:
                if claim():
                    os.replace(tmp, target)
                    return True
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return False

    def _other_voices(self, character):
        return [self.store.voice_path(c) for c in self.store.characters(character['book_id'])
                if c['id'] != character['id'] and c['voice_state'] == 'ready']

    def design_voice(self, character):
        rev, book_id = character['voice_rev'], character['book_id']
        claimed = self.store.run("UPDATE character SET voice_state = 'designing', error = NULL "
                                 "WHERE id = ? AND voice_rev = ? AND voice_state = 'none'", (character['id'], rev)).rowcount
        if not claimed:
            return
        self._publish_cast(book_id)
        self._say('speech', f"Designing a voice for {character['name']}")
        try:
            audio, rate = self.engine.design(speech.voice_instruction(character), seed=character['id'] * 1000 + rev,
                                             language=self._language(book_id), others=self._other_voices(character))
            self._land(lambda tmp: sf.write(tmp, audio, rate, format='WAV'), self.store.voice_path(character),
                       lambda: self.store.run("UPDATE character SET voice_state = 'ready' WHERE id = ? AND voice_rev = ? "
                                              "AND voice_state = 'designing'", (character['id'], rev)).rowcount)
        except Exception as e:
            log.exception('voice design failed for %s', character['name'])
            self.store.run("UPDATE character SET voice_state = 'failed', error = ? WHERE id = ? AND voice_rev = ? "
                           "AND voice_state = 'designing'", (str(e)[:300], character['id'], rev))
        self._publish_cast(book_id)

    def design_draft(self, character):
        """Design a candidate sample next to the current voice, for the reader to hear before approving."""
        rev, book_id = character['draft_rev'], character['book_id']
        if not self.store.run("UPDATE character SET draft_state = 'designing' WHERE id = ? AND draft_rev = ? "
                              "AND draft_state = 'queued'", (character['id'], rev)).rowcount:
            return
        self._publish_cast(book_id)
        self._say('speech', f"Designing a candidate voice for {character['name']}")
        try:
            audio, rate = self.engine.design(speech.voice_instruction(character), seed=character['id'] * 1000 + 500 + rev,
                                             language=self._language(book_id), others=self._other_voices(character))
            self._land(lambda tmp: sf.write(tmp, audio, rate, format='WAV'), self.store.draft_path(character),
                       lambda: self.store.run("UPDATE character SET draft_state = 'ready' WHERE id = ? AND draft_rev = ? "
                                              "AND draft_state = 'designing'", (character['id'], rev)).rowcount)
        except Exception:
            log.exception('candidate voice failed for %s', character['name'])
            self.store.run("UPDATE character SET draft_state = 'failed' WHERE id = ? AND draft_rev = ? "
                           "AND draft_state = 'designing'", (character['id'], rev))
        self._publish_cast(book_id)

    def make_take(self, character, take):
        """Have Breeze direct the character's voice into a take of an emotion. When the take was asked for by hand, the
        lines already read in that emotion are read again once it lands."""
        character_id, emotion, rev, book_id = character['id'], take['emotion'], take['rev'], character['book_id']
        self.store.run('INSERT OR IGNORE INTO voice_take (character_id, emotion) VALUES (?, ?)', (character_id, emotion))
        if not self.store.run("UPDATE voice_take SET state = 'making', made_from = ?, error = NULL WHERE character_id = ? "
                              "AND emotion = ? AND rev = ? AND state != 'making'",
                              (character['voice_rev'], character_id, emotion, rev)).rowcount:
            return
        self._publish_cast(book_id)
        self._say('speech', f"Directing {character['name']}'s voice: {emotion}")
        def settle(state, error=None):
            return self.store.run('UPDATE voice_take SET state = ?, error = ? WHERE character_id = ? AND emotion = ? '
                                  "AND rev = ? AND state = 'making'", (state, error, character_id, emotion, rev)).rowcount
        try:
            audio, rate = self.engine.direct(self.store.voice_path(character), emotion,
                                             seed=character_id * 1000 + 700 + rev)
            landed = self._land(lambda tmp: sf.write(tmp, audio, rate, format='WAV'),
                                self.store.take_path(character, emotion), lambda: settle('ready'))
            if landed and take['state'] == 'queued':
                where = "character_id = ? AND emotion = ? AND kind = 'speech'", (character_id, emotion)
                self.store.invalidate_audio(*where)
                self._publish_segments(book_id, *where)
        except Exception as e:
            log.exception('the %s take failed for %s', emotion, character['name'])
            settle('failed', str(e)[:300])
        self._publish_cast(book_id)

    def generate(self, book_id, seg, character, emotion='neutral'):
        claim = (seg['id'], seg['audio_rev'])
        if not self.store.run("UPDATE segment SET audio_state = 'generating' WHERE id = ? AND audio_rev = ? "
                              "AND audio_state = 'none'", claim).rowcount:
            return
        self._publish_segments(book_id, 'id = ?', (seg['id'],))
        self._say('speech', f"Reading as {character['name']}" + ('' if emotion == 'neutral' else f', {emotion}'))
        voice = self.store.voice_path(character) if emotion == 'neutral' else self.store.take_path(character, emotion)
        try:
            text = speakable(seg['text'])
            if text:
                audio, rate = self.engine.speak(text, voice, seed=seg['audio_rev'] * 1000 + 42,
                                                language=self._language(book_id))
            else:
                audio, rate = np.zeros(int(SILENCE[0] * SILENCE[1]), dtype='float32'), SILENCE[1]
            duration = len(audio) / rate
            self._land(lambda tmp: sf.write(tmp, audio, rate, format='FLAC', subtype='PCM_16'),
                       self.store.audio_path(book_id, seg['id']),
                       lambda: self.store.run("UPDATE segment SET audio_state = 'ready', duration = ? WHERE id = ? "
                                              "AND audio_rev = ? AND audio_state = 'generating'", (duration, *claim)).rowcount)
        except speech.Unavailable as e:
            log.warning('the speech server is unavailable for segment %s: %s', seg['id'], e)
            self.store.run("UPDATE segment SET audio_state = 'none' WHERE id = ? AND audio_rev = ? "
                           "AND audio_state = 'generating'", claim)
            self.idle['speech'] = 'The speech server is unavailable; trying again shortly'
            self.retry_at['speech'] = time.monotonic() + SPEECH_RETRY
        except Exception as e:
            log.exception('speech failed for segment %s', seg['id'])
            self.store.run("UPDATE segment SET audio_state = 'failed', error = ? WHERE id = ? AND audio_rev = ? "
                           "AND audio_state = 'generating'", (str(e)[:300], *claim))
        self._publish_segments(book_id, 'id = ?', (seg['id'],))

    def find_appearance(self, character):
        """Ask how a character looks from every paragraph of the book about them, and save it. Raises TaggerError."""
        book_id, settings = character['book_id'], self.store.settings()
        book = self.store.one('SELECT title, author, language FROM book WHERE id = ?', (book_id,))
        text, spoken = {}, set()
        for seg in self.store.all('SELECT block_idx, text, character_id FROM segment WHERE book_id = ? '
                                  'ORDER BY block_idx, seq', (book_id,)):
            text[seg['block_idx']] = text.get(seg['block_idx'], '') + seg['text']
            if seg['character_id'] == character['id']:
                spoken.add(seg['block_idx'])
        paragraphs = scenes.mentioning(text.items(), character['name'], spoken)
        if not paragraphs:
            raise tagger.TaggerError(f"the book never mentions {character['name']}")
        answer, cost = self.ask(settings, scenes.look_prompt(book, character, paragraphs), self.llm_cwd,
                                system=scenes.LOOK_SYSTEM, schema=scenes.LOOK_SCHEMA)
        self.store.record_usage(settings['provider'], settings[f"{settings['provider']}_model"], len(paragraphs), cost)
        appearance = scenes.look_from(answer)
        if not appearance:
            raise tagger.TaggerError('the answer described nothing')
        return self.store.update_character(character['id'], {'appearance': appearance})

    def star(self, block):
        """Have the picture of a paragraph drawn whatever the page limit says, asking what it shows when it has none.
        Returns the moment. Raises TaggerError."""
        book_id, idx, settings = block['book_id'], block['idx'], self.store.settings()
        star = "UPDATE moment SET starred = 1, wanted = 1 WHERE block_id = ? AND kind = 'picture'", (block['id'],)
        if not self.store.run(*star).rowcount:
            book = self.store.one('SELECT title, author, language FROM book WHERE id = ?', (book_id,))
            prompt, markers = scenes.star_prompt(book, self.store.characters(book_id),
                                                 self.store.blocks(book_id, idx - CONTEXT_BLOCKS, idx - 1),
                                                 self.store.blocks(book_id, idx, idx)[0])
            answer, cost = self.ask(settings, prompt, self.llm_cwd, system=scenes.STAR_SYSTEM, schema=scenes.SCHEMA)
            self.store.record_usage(settings['provider'], settings[f"{settings['provider']}_model"], 1, cost)
            # In one transaction, so that an answer with nothing to draw leaves the end of the last picture in place.
            with self.store.lock:
                starred = 0
                self.store.run('BEGIN')
                try:
                    self.store.run("DELETE FROM moment WHERE block_id = ? AND kind = 'clear'", (block['id'],))
                    scenes.apply(self.store, book_id, markers, {**answer, 'ends': []}, 1)
                    starred = self.store.run(*star).rowcount
                finally:
                    self.store.run('COMMIT' if starred else 'ROLLBACK')
            if not starred:
                raise tagger.TaggerError('the answer described nothing to draw in this paragraph')
        self.store.redraw_moments("m.block_id = ? AND m.image_state = 'failed'", (block['id'],))
        return self.store.moments_where('m.block_id = ?', (block['id'],))[0]

    # Pictures

    def image_step(self):
        """Draw the next missing picture: one the reader starred, else one on the current or next page. The portrait
        of someone in it is drawn first when needed."""
        focus = self.focus
        settings = self.store.settings()
        if not focus or not illustrating(settings):
            self.idle['images'] = None
            return False
        if time.monotonic() < self.retry_at.get('images', 0):
            return False
        book_id = focus['book_id']
        problem = self._draw_next(book_id, focus, settings)
        if problem is None:
            return False
        if problem:
            self.idle['images'] = f'The image service {problem}; trying again shortly'
            self.retry_at['images'] = time.monotonic() + IMAGE_RETRY
            return False
        self.idle['images'] = None
        return True

    def _draw_next(self, book_id, focus, settings):
        """None when nothing needs drawing, otherwise what kept the image service from answering ('' when it answered)."""
        url = settings['image_url']
        # A starred picture is drawn wherever it is in the book, and takes none of its page's places.
        starred = self.store.one("SELECT id, scene, image_state, image_rev FROM moment WHERE book_id = ? AND starred "
                                 "AND kind = 'picture' AND image_state = 'none' ORDER BY id LIMIT 1", (book_id,))
        if starred:
            return self._draw_scene(book_id, starred, url)
        pages = (focus['current_first'], focus['current_last']), (focus['current_last'] + 1, focus['last'])
        # Of a page's moments the ones that matter most count, and they are drawn in reading order.
        counted = [sorted(self.store.all(
            "SELECT id, scene, image_state, image_rev, block_idx FROM moment WHERE book_id = ? AND kind = 'picture' "
            "AND NOT starred AND block_idx BETWEEN ? AND ? ORDER BY importance DESC, block_idx LIMIT ?",
            (book_id, first, last, settings['pictures_per_page'])), key=lambda m: m['block_idx']) for first, last in pages]
        ids = [m['id'] for page in counted for m in page]
        self.store.run(f"UPDATE moment SET wanted = 1 WHERE wanted = 0 AND id IN ({','.join('?' * len(ids))})", ids)
        # The picture on screen now is the last one behind the reader, when the page was turned before it was drawn
        # too. Nothing else behind the reader is drawn, and nothing is carried into a new chapter.
        carried = self.store.one(
            "SELECT id, kind, scene, image_state, image_rev FROM moment WHERE book_id = ? AND block_idx < ? "
            "AND block_idx >= (SELECT MAX(first_block) FROM chapter WHERE book_id = ? AND first_block <= ?) "
            "AND (kind = 'clear' OR wanted) ORDER BY block_idx DESC LIMIT 1",
            (book_id, focus['current_first'], book_id, focus['current_first']))
        shown = [carried] if carried and carried['kind'] == 'picture' else []
        moment = next((m for m in shown + counted[0] + counted[1] if m['image_state'] == 'none'), None)
        if moment:
            return self._draw_scene(book_id, moment, url)
        undrawn = self.store.one(
            "SELECT id, book_id, name, gender, age, appearance, portrait_rev FROM character WHERE book_id = ? "
            "AND role != 'narrator' AND appearance != '' AND portrait_state = 'none' ORDER BY id LIMIT 1", (book_id,))
        return self.draw_portrait(undrawn, url) if undrawn else None

    def _draw_scene(self, book_id, moment, url):
        """Draw a moment, or first the missing portrait of someone in it."""
        present = self.store.all(
            "SELECT c.id, c.book_id, c.name, c.gender, c.age, c.appearance, c.portrait_state, c.portrait_rev "
            "FROM moment_character mc JOIN character c ON c.id = mc.character_id "
            "WHERE mc.moment_id = ? AND c.role != 'narrator' ORDER BY mc.pos LIMIT ?", (moment['id'], scenes.MAX_CHARACTERS))
        undrawn = next((c for c in present if c['appearance'] and c['portrait_state'] == 'none'), None)
        return self.draw_portrait(undrawn, url) if undrawn else self.draw_moment(book_id, moment, present, url)

    def draw_portrait(self, character, url):
        return self._draw(PORTRAIT, character['id'], character['portrait_rev'], self.store.portrait_path(character),
                          f"Drawing {character['name']}", lambda: self._publish_cast(character['book_id']),
                          url, images.portrait_prompt(character), images.PORTRAIT_SIZE)

    def draw_moment(self, book_id, moment, present, url):
        ready = [c for c in present if c['portrait_state'] == 'ready' and self.store.portrait_path(c).exists()]
        described = [(c['name'], c['appearance']) for c in present if c not in ready and c['appearance']]
        return self._draw(MOMENT, moment['id'], moment['image_rev'], self.store.moment_path(book_id, moment['id']),
                          'Drawing the scene', lambda: self._publish_moments(book_id, 'm.id = ?', (moment['id'],)),
                          url, images.scene_prompt(moment['scene'], [c['name'] for c in ready], described), images.SCENE_SIZE,
                          [self.store.portrait_path(c).read_bytes() for c in ready])

    def _draw(self, kind, row_id, rev, target, saying, announce, url, prompt, size, references=()):
        table, state, revision, error = kind
        claim = f'id = ? AND {revision} = ?', (row_id, rev)

        def move(before, after, problem=None):
            return self.store.run(f"UPDATE {table} SET {state} = ?, {error} = ? WHERE {claim[0]} AND {state} = ?",
                                  (after, problem, *claim[1], before)).rowcount
        if not move('none', 'drawing'):
            return ''
        announce()
        self._say('images', saying)
        unavailable = ''
        try:
            png = self.draw(url, prompt, *size, row_id * 1000 + rev, references)
            self._land(lambda tmp: Path(tmp).write_bytes(png), target, lambda: move('drawing', 'ready'))
        except images.Unavailable as e:
            log.warning('the image service %s', e)
            move('drawing', 'none')
            unavailable = str(e)
        except Exception as e:
            log.exception('drawing failed for %s %s', table, row_id)
            move('drawing', 'failed', str(e)[:300])
        announce()
        return unavailable
