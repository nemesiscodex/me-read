import { describe, expect, it } from 'vitest'
import { initialPlayer, step, type PlayerContext, type PlayerEvent, type PlayerState } from './player'
import type { Playability } from './segments'

function chapter(playabilities: Playability[], hasNextChapter = false): PlayerContext {
  return { items: playabilities.map((playability, i) => ({ id: i + 1, playability })), hasNextChapter }
}

function run(ctx: PlayerContext, events: PlayerEvent[], from: PlayerState = initialPlayer): PlayerState {
  return events.reduce((state, event) => step(state, event, ctx), from)
}

const ended = (state: PlayerState): PlayerEvent => {
  if (state.status !== 'playing') throw new Error(`expected playing, got ${state.status}`)
  return { type: 'ended', segmentId: state.segmentId, run: state.run }
}

describe('player', () => {
  it('plays a ready line when it is clicked', () => {
    expect(run(chapter(['ready', 'ready']), [{ type: 'play', segmentId: 2 }])).toMatchObject({
      status: 'playing',
      segmentId: 2,
    })
  })

  it('waits on a clicked line that is still being prepared and ignores one that never will be', () => {
    const ctx = chapter(['pending', 'skip'])
    expect(run(ctx, [{ type: 'play', segmentId: 1 }])).toMatchObject({ status: 'waiting', segmentId: 1 })
    expect(run(ctx, [{ type: 'play', segmentId: 2 }])).toEqual(initialPlayer)
  })

  it('moves to the next line when a clip ends, skipping lines without audio', () => {
    const ctx = chapter(['ready', 'skip', 'ready'])
    const playing = run(ctx, [{ type: 'play', segmentId: 1 }])
    expect(step(playing, ended(playing), ctx)).toMatchObject({ status: 'playing', segmentId: 3 })
  })

  it('waits for the next line, then continues by itself once it is ready', () => {
    const playing = run(chapter(['ready', 'pending']), [{ type: 'play', segmentId: 1 }])
    const waiting = step(playing, ended(playing), chapter(['ready', 'pending']))
    expect(waiting).toMatchObject({ status: 'waiting', segmentId: 2 })

    expect(step(waiting, { type: 'sync' }, chapter(['ready', 'pending']))).toBe(waiting)
    expect(step(waiting, { type: 'sync' }, chapter(['ready', 'ready']))).toMatchObject({
      status: 'playing',
      segmentId: 2,
    })
  })

  it('moves past a line it was waiting for when that line fails', () => {
    const waiting = run(chapter(['pending', 'ready']), [{ type: 'play', segmentId: 1 }])
    expect(step(waiting, { type: 'sync' }, chapter(['skip', 'ready']))).toMatchObject({
      status: 'playing',
      segmentId: 2,
    })
  })

  it('stops at the end of the last chapter', () => {
    const ctx = chapter(['ready'])
    const playing = run(ctx, [{ type: 'play', segmentId: 1 }])
    expect(step(playing, ended(playing), ctx).status).toBe('idle')
  })

  it('continues into the next chapter from its first playable line', () => {
    const playing = run(chapter(['ready'], true), [{ type: 'play', segmentId: 1 }])
    const turning = step(playing, ended(playing), chapter(['ready'], true))
    expect(turning.status).toBe('turning')

    const next: PlayerContext = { items: [{ id: 50, playability: 'skip' }, { id: 51, playability: 'ready' }], hasNextChapter: false }
    expect(run(next, [{ type: 'sync' }, { type: 'chapterLoaded' }], turning)).toMatchObject({
      status: 'playing',
      segmentId: 51,
    })
  })

  it('stops when the chapter under the playing line is replaced by another', () => {
    const playing = run(chapter(['ready']), [{ type: 'play', segmentId: 1 }])
    const other: PlayerContext = { items: [{ id: 50, playability: 'ready' }], hasNextChapter: false }
    expect(run(other, [{ type: 'sync' }, { type: 'chapterLoaded' }], playing).status).toBe('idle')
  })

  it('ignores an ended event from a clip it already left', () => {
    const ctx = chapter(['ready', 'ready', 'ready'])
    const first = run(ctx, [{ type: 'play', segmentId: 1 }])
    const second = step(first, { type: 'play', segmentId: 2 }, ctx)
    expect(step(second, ended(first), ctx)).toBe(second)
  })

  it('pauses and resumes the same clip without restarting it', () => {
    const ctx = chapter(['ready'])
    const playing = run(ctx, [{ type: 'play', segmentId: 1 }])
    const paused = step(playing, { type: 'toggle', from: null }, ctx)
    expect(paused).toEqual({ status: 'paused', segmentId: 1, run: playing.run })
    expect(step(paused, { type: 'toggle', from: null }, ctx)).toEqual(playing)
  })

  it('starts a fresh run when the same line is played again, so a regenerated clip is picked up', () => {
    const ctx = chapter(['ready'])
    const playing = run(ctx, [{ type: 'play', segmentId: 1 }])
    const again = step(playing, { type: 'play', segmentId: 1 }, ctx)
    expect(again).toMatchObject({ status: 'playing', segmentId: 1 })
    expect(again.run).toBeGreaterThan(playing.run)
  })

  it('waits when resuming a line that went back to being prepared', () => {
    const paused = run(chapter(['ready']), [{ type: 'play', segmentId: 1 }, { type: 'toggle', from: null }])
    expect(step(paused, { type: 'toggle', from: null }, chapter(['pending']))).toMatchObject({
      status: 'waiting',
      segmentId: 1,
    })
  })

  it('starts from the given line when toggled while idle', () => {
    const ctx = chapter(['ready', 'skip', 'ready'])
    expect(step(initialPlayer, { type: 'toggle', from: 2 }, ctx)).toMatchObject({ status: 'playing', segmentId: 3 })
    expect(step(initialPlayer, { type: 'toggle', from: null }, ctx)).toBe(initialPlayer)
  })

  it('skips forward and back over lines without audio, and restarts the first line when there is nothing before it', () => {
    const ctx = chapter(['ready', 'skip', 'ready'])
    const first = run(ctx, [{ type: 'play', segmentId: 1 }])
    const third = step(first, { type: 'skip', dir: 1 }, ctx)
    expect(third).toMatchObject({ status: 'playing', segmentId: 3 })
    const back = step(third, { type: 'skip', dir: -1 }, ctx)
    expect(back).toMatchObject({ status: 'playing', segmentId: 1 })
    const restarted = step(back, { type: 'skip', dir: -1 }, ctx)
    expect(restarted).toMatchObject({ status: 'playing', segmentId: 1 })
    expect(restarted.run).toBeGreaterThan(back.run)
  })

  it('pauses on a clip that fails to load instead of racing through the chapter', () => {
    const ctx = chapter(['ready', 'ready'])
    const playing = run(ctx, [{ type: 'play', segmentId: 1 }])
    expect(step(playing, { type: 'clipFailed', segmentId: 1, run: playing.run }, ctx)).toMatchObject({
      status: 'paused',
      segmentId: 1,
    })
  })

  it('pauses while waiting, and stays paused when the line becomes ready', () => {
    const waiting = run(chapter(['pending']), [{ type: 'play', segmentId: 1 }])
    const paused = step(waiting, { type: 'toggle', from: null }, chapter(['pending']))
    expect(paused.status).toBe('paused')
    expect(step(paused, { type: 'sync' }, chapter(['ready']))).toBe(paused)
  })
})
