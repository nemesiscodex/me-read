"""Background work for the pages the reader is on: tag spoken lines, design voices, generate clips.

Two workers each repeat one step: look in the database for the most useful next job inside the reader's
focus, do it, record the result. Nothing is queued in memory, so a restart or a correction just changes what
the next lookup finds.
"""
import logging
import os
import tempfile
import threading

import numpy as np
import soundfile as sf

from . import speech, tagger
from .segment import speakable

log = logging.getLogger('meread')
TAG_BATCH = 20
CONTEXT_BLOCKS = 8
MAX_FOCUS = 400
SILENCE = 0.6, 24000


class Pipeline:
    def __init__(self, store, engine, ask=tagger.ask, publish=lambda book_id, event: None):
        self.store, self.engine, self.ask, self.publish = store, engine, ask, publish
        self.focus = None
        self.activity = {'tagger': None, 'speech': None}
        self.idle = {'tagger': None, 'speech': None}
        self.wake = {'tagger': threading.Event(), 'speech': threading.Event()}
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
        for name, step in (('tagger', self.tag_step), ('speech', self.speech_step)):
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
        return True

    # Voices and clips

    def speech_step(self):
        """Generate the next missing clip in focus, designing its voice first when needed."""
        draft = self.store.one("SELECT id FROM character WHERE draft_state = 'queued' ORDER BY id LIMIT 1")
        if draft:
            self.design_draft(self.store.character(draft['id']))
            return True
        focus = self.focus
        if not focus:
            return False
        book_id = focus['book_id']
        # While tagging is paused the narrator reads lines that have no speaker yet.
        paused = self.store.settings()['tagging_paused']
        seg = self.store.one(
            "SELECT s.id, s.text, s.audio_rev, s.character_id FROM segment s "
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
                self.generate(book_id, seg, character)
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
                                             language=self._language(book_id))
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
                                             language=self._language(book_id))
            self._land(lambda tmp: sf.write(tmp, audio, rate, format='WAV'), self.store.draft_path(character),
                       lambda: self.store.run("UPDATE character SET draft_state = 'ready' WHERE id = ? AND draft_rev = ? "
                                              "AND draft_state = 'designing'", (character['id'], rev)).rowcount)
        except Exception:
            log.exception('candidate voice failed for %s', character['name'])
            self.store.run("UPDATE character SET draft_state = 'failed' WHERE id = ? AND draft_rev = ? "
                           "AND draft_state = 'designing'", (character['id'], rev))
        self._publish_cast(book_id)

    def generate(self, book_id, seg, character):
        claim = (seg['id'], seg['audio_rev'])
        if not self.store.run("UPDATE segment SET audio_state = 'generating' WHERE id = ? AND audio_rev = ? "
                              "AND audio_state = 'none'", claim).rowcount:
            return
        self._publish_segments(book_id, 'id = ?', (seg['id'],))
        self._say('speech', f"Reading as {character['name']}")
        try:
            text = speakable(seg['text'])
            if text:
                audio, rate = self.engine.speak(text, self.store.voice_path(character), seed=seg['audio_rev'] * 1000 + 42,
                                                language=self._language(book_id))
            else:
                audio, rate = np.zeros(int(SILENCE[0] * SILENCE[1]), dtype='float32'), SILENCE[1]
            duration = len(audio) / rate
            self._land(lambda tmp: sf.write(tmp, audio, rate, format='FLAC', subtype='PCM_16'),
                       self.store.audio_path(book_id, seg['id']),
                       lambda: self.store.run("UPDATE segment SET audio_state = 'ready', duration = ? WHERE id = ? "
                                              "AND audio_rev = ? AND audio_state = 'generating'", (duration, *claim)).rowcount)
        except Exception as e:
            log.exception('speech failed for segment %s', seg['id'])
            self.store.run("UPDATE segment SET audio_state = 'failed', error = ? WHERE id = ? AND audio_rev = ? "
                           "AND audio_state = 'generating'", (str(e)[:300], *claim))
        self._publish_segments(book_id, 'id = ?', (seg['id'],))
