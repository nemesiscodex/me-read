"""The life cycle every kind of speech and picture work shares: designing a voice or a candidate voice, making a take,
reading a line or a clip, drawing a portrait or a moment.

A job is a database row whose state column says where its work is. A worker claims a waiting job and does the work.
Then the job lands (its file moves into place and it is ready), goes back to wait when the service did not answer, or
fails with the reason. A claim holds one revision of the row, so a change that bumps the revision or puts the job back
makes the work in flight land nowhere. A restart puts the work it interrupted back to wait.
"""
import os
import tempfile
import time
from dataclasses import dataclass

# Leaves the error column as it is.
KEEP = object()


@dataclass(frozen=True)
class Job:
    table: str
    key: tuple[str, ...]
    state: str
    # None when the state alone guards a claim.
    rev: str | None
    error: str
    # A claim takes the job from `waiting` to `working`. It lands in 'ready', fails in 'failed', and a service that did
    # not answer puts it back in `retry`.
    waiting: tuple[str, ...]
    working: str
    retry: str
    error_length: int = 300
    # The state a restart puts work back in, and the states it applies to. Without it, `working` goes back to `retry`.
    restart: tuple[str, tuple[str, ...]] | None = None
    # What the error column says on a claim, on a retry (`{}` is the reason) and on a restart. None clears it.
    claim_note: object = None
    retry_note: str | None = None
    restart_note: object = KEEP
    # A column that records when the job last changed.
    stamp: str | None = None

    def claim(self, store, key, rev=None, **columns):
        """Take a waiting job, writing `columns` too. None when it is not waiting at this revision."""
        claim = Claim(self, store, key if isinstance(key, tuple) else (key,), rev)
        return claim if claim._move(self.waiting, self.working, self.claim_note, columns) else None

    def refuse(self, store, where, args, reason):
        """Fail the waiting jobs matching `where` without doing them, keeping the whole reason. Returns how many."""
        return self._move(store, where, args, self.waiting, 'failed', reason)

    def recover(self, store):
        """Put the work a restart interrupted back to wait."""
        state, interrupted = self.restart or (self.retry, (self.working,))
        self._move(store, '1', (), interrupted, state, self.restart_note)

    def _move(self, store, where, args, before, after, note, columns=None):
        """Move the rows matching `where` that are in a `before` state to `after`. Returns how many moved."""
        changes = {self.state: after, **(columns or {})}
        if note is not KEEP:
            changes[self.error] = note
        if self.stamp:
            changes[self.stamp] = time.time()
        sets, marks = ', '.join(f'{column} = ?' for column in changes), ','.join('?' * len(before))
        return store.run(f'UPDATE {self.table} SET {sets} WHERE {where} AND {self.state} IN ({marks})',
                         (*changes.values(), *args, *before)).rowcount


class Claim:
    """A worker's hold on one job at one revision. Each change holds only while the job is still being worked on."""

    def __init__(self, job, store, key, rev):
        self.job, self.store = job, store
        held = dict(zip(job.key, key)) | ({job.rev: rev} if job.rev else {})
        self.where, self.args = ' AND '.join(f'{column} = ?' for column in held), tuple(held.values())

    def progress(self, note):
        """Say how the work is going. False once the claim no longer holds."""
        return bool(self._move((self.job.working,), self.job.working, note))

    def land(self, write, target, **columns):
        """Write the result next to `target` and move it into place, ready, if the claim still holds."""
        return land(self.store, write, target, lambda: self._move((self.job.working,), 'ready', None, columns))

    def retry(self, reason):
        """Put the job back to wait for a service that did not answer."""
        note = None if self.job.retry_note is None else self.job.retry_note.format(reason)
        self._move((self.job.working,), self.job.retry, note)

    def fail(self, error):
        self._move((self.job.working,), 'failed', str(error)[:self.job.error_length])

    def _move(self, before, after, note, columns=None):
        return self.job._move(self.store, self.where, self.args, before, after, note, columns)


def land(store, write, target, holds):
    """Write a file next to `target` and move it into place only if `holds()` still does in the database."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=target.suffix)
    os.close(fd)
    try:
        write(tmp)
        with store.lock:
            if holds():
                os.replace(tmp, target)
                return True
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return False


VOICE = Job('character', ('id',), 'voice_state', 'voice_rev', 'error',
            waiting=('none',), working='designing', retry='none')
# The reader watches a candidate voice being designed, so its error column is a status line, stamped at every change.
DRAFT = Job('character', ('id',), 'draft_state', 'draft_rev', 'draft_status',
            waiting=('queued',), working='designing', retry='queued', error_length=600,
            claim_note='Starting voice design…', retry_note='{} Retrying automatically.',
            restart_note='Interrupted by restart; waiting for the speech worker.', stamp='draft_updated_at')
# A take ready or failed from an older voice counts as never made, so it can be made again.
TAKE = Job('voice_take', ('character_id', 'emotion'), 'state', 'rev', 'error',
           waiting=('none', 'queued', 'ready', 'failed'), working='making', retry='queued')
AUDIO = Job('segment', ('id',), 'audio_state', 'audio_rev', 'error',
            waiting=('none',), working='generating', retry='none')
# Clips wait for their client to ask again, as the reader's focus does, and a failed clip gets another try. A clip
# asked for again keeps its old error until it is read.
CLIP = Job('clip', ('key',), 'state', None, 'error',
           waiting=('queued',), working='generating', retry='queued',
           restart=('idle', ('queued', 'generating', 'failed')), claim_note=KEEP)
PORTRAIT = Job('character', ('id',), 'portrait_state', 'portrait_rev', 'portrait_error',
               waiting=('none',), working='drawing', retry='none')
MOMENT = Job('moment', ('id',), 'image_state', 'image_rev', 'error',
             waiting=('none',), working='drawing', retry='none')
JOBS = VOICE, DRAFT, TAKE, AUDIO, CLIP, PORTRAIT, MOMENT
