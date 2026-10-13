import type { PlayerEvent, PlayerState } from './player'

/** The system controls the reader answers: keyboard media keys, Now Playing, and headset buttons. */
export const MEDIA_KEYS = ['play', 'pause', 'stop', 'nexttrack', 'previoustrack'] as const
export type MediaKey = (typeof MEDIA_KEYS)[number]

export function isRunning(state: PlayerState): boolean {
  return state.status === 'playing' || state.status === 'waiting' || state.status === 'turning'
}

/**
 * The player event for a system media control. Play and pause are one-way, unlike the toggle
 * button, because a headset may send either while the other is already true. Stop pauses, so the
 * reader keeps its place.
 */
export function mediaKeyEvent(state: PlayerState, key: MediaKey, from: number | null): PlayerEvent | null {
  switch (key) {
    case 'play':
      return isRunning(state) ? null : { type: 'toggle', from }
    case 'pause':
    case 'stop':
      return isRunning(state) ? { type: 'toggle', from } : null
    case 'nexttrack':
      return { type: 'skip', dir: 1 }
    case 'previoustrack':
      return { type: 'skip', dir: -1 }
  }
}
