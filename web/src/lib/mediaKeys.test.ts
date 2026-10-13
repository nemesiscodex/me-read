import { describe, expect, it } from 'vitest'
import { mediaKeyEvent } from './mediaKeys'
import { step, type PlayerContext, type PlayerState } from './player'

const ctx: PlayerContext = {
  items: [
    { id: 1, playability: 'ready' },
    { id: 2, playability: 'ready' },
    { id: 3, playability: 'ready' },
  ],
  hasNextChapter: false,
}
const playing: PlayerState = { status: 'playing', segmentId: 2, run: 1 }
const paused: PlayerState = { status: 'paused', segmentId: 2, run: 1 }
const idle: PlayerState = { status: 'idle', run: 0 }

function press(state: PlayerState, key: Parameters<typeof mediaKeyEvent>[1], from: number | null = 1): PlayerState {
  const event = mediaKeyEvent(state, key, from)
  return event ? step(state, event, ctx) : state
}

describe('media keys', () => {
  it('pause stops a playing line and keeps it, and pressing it again changes nothing', () => {
    expect(press(playing, 'pause')).toEqual(paused)
    expect(press(paused, 'pause')).toEqual(paused)
  })

  it('stop pauses instead of losing the place', () => {
    expect(press(playing, 'stop')).toEqual(paused)
  })

  it('play resumes the paused line, and pressing it while playing changes nothing', () => {
    expect(press(paused, 'play')).toEqual(playing)
    expect(press(playing, 'play')).toEqual(playing)
  })

  it('play from idle starts at the line the reader is on', () => {
    expect(press(idle, 'play', 3)).toMatchObject({ status: 'playing', segmentId: 3 })
  })

  it('next and previous move one line', () => {
    expect(press(playing, 'nexttrack')).toMatchObject({ status: 'playing', segmentId: 3 })
    expect(press(playing, 'previoustrack')).toMatchObject({ status: 'playing', segmentId: 1 })
  })
})
