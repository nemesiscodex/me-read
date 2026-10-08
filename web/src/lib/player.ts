import type { PlayItem } from './segments'

/**
 * `run` counts fresh starts. A resume keeps it, so the audio driver continues the same clip;
 * any new start bumps it, so the driver loads the clip again from the top.
 */
export type PlayerState =
  | { status: 'idle'; run: number }
  | { status: 'playing'; segmentId: number; run: number }
  | { status: 'paused'; segmentId: number; run: number }
  | { status: 'waiting'; segmentId: number; run: number }
  | { status: 'turning'; run: number }

export type PlayerEvent =
  | { type: 'play'; segmentId: number }
  | { type: 'toggle'; from: number | null }
  | { type: 'skip'; dir: 1 | -1 }
  | { type: 'ended'; segmentId: number; run: number }
  | { type: 'clipFailed'; segmentId: number; run: number }
  | { type: 'sync' }
  | { type: 'chapterLoaded' }
  | { type: 'stop' }

export interface PlayerContext {
  /** The loaded chapter in reading order. */
  items: PlayItem[]
  hasNextChapter: boolean
}

export const initialPlayer: PlayerState = { status: 'idle', run: 0 }

export function activeSegmentId(state: PlayerState): number | null {
  return 'segmentId' in state ? state.segmentId : null
}

function seek(ctx: PlayerContext, from: number, dir: 1 | -1): PlayItem | null {
  for (let i = from; i >= 0 && i < ctx.items.length; i += dir) {
    const item = ctx.items[i]!
    if (item.playability !== 'skip') return item
  }
  return null
}

function start(item: PlayItem, run: number): PlayerState {
  return item.playability === 'ready'
    ? { status: 'playing', segmentId: item.id, run }
    : { status: 'waiting', segmentId: item.id, run }
}

function forward(ctx: PlayerContext, from: number, run: number): PlayerState {
  const item = seek(ctx, from, 1)
  if (item) return start(item, run)
  return ctx.hasNextChapter ? { status: 'turning', run } : { status: 'idle', run }
}

function toggle(state: PlayerState, from: number | null, ctx: PlayerContext): PlayerState {
  const run = state.run + 1
  const indexOf = (id: number | null) => ctx.items.findIndex((item) => item.id === id)
  switch (state.status) {
    case 'playing':
    case 'waiting':
      return { status: 'paused', segmentId: state.segmentId, run: state.run }
    case 'paused': {
      const at = indexOf(state.segmentId)
      if (at === -1) return { status: 'idle', run }
      return ctx.items[at]!.playability === 'ready'
        ? { status: 'playing', segmentId: state.segmentId, run: state.run }
        : forward(ctx, at, run)
    }
    case 'turning':
      return { status: 'idle', run }
    case 'idle': {
      const at = indexOf(from)
      return at === -1 ? state : forward(ctx, at, run)
    }
  }
}

export function step(state: PlayerState, event: PlayerEvent, ctx: PlayerContext): PlayerState {
  const run = state.run + 1
  const indexOf = (id: number) => ctx.items.findIndex((item) => item.id === id)

  switch (event.type) {
    case 'play': {
      const item = ctx.items[indexOf(event.segmentId)]
      return item && item.playability !== 'skip' ? start(item, run) : state
    }

    case 'toggle':
      return toggle(state, event.from, ctx)

    case 'skip': {
      if (!('segmentId' in state)) return state
      const at = indexOf(state.segmentId)
      if (at === -1) return { status: 'idle', run }
      if (event.dir === 1) return forward(ctx, at + 1, run)
      const item = seek(ctx, at - 1, -1) ?? ctx.items[at]!
      return item.playability === 'skip' ? state : start(item, run)
    }

    case 'ended': {
      if (state.status !== 'playing' || state.segmentId !== event.segmentId || state.run !== event.run) return state
      const at = indexOf(state.segmentId)
      return at === -1 ? { status: 'idle', run } : forward(ctx, at + 1, run)
    }

    case 'clipFailed':
      return state.status === 'playing' && state.segmentId === event.segmentId && state.run === event.run
        ? { status: 'paused', segmentId: state.segmentId, run: state.run }
        : state

    case 'sync': {
      if (!('segmentId' in state)) return state
      const at = indexOf(state.segmentId)
      if (at === -1) return { status: 'idle', run }
      if (state.status !== 'waiting') return state
      return ctx.items[at]!.playability === 'pending' ? state : forward(ctx, at, run)
    }

    case 'chapterLoaded':
      return state.status === 'turning' ? forward(ctx, 0, run) : state

    case 'stop':
      return state.status === 'idle' ? state : { status: 'idle', run }
  }
}
