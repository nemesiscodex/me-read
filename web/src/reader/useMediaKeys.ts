import { useEffect, useRef } from 'react'
import { flushSync } from 'react-dom'
import { isRunning, MEDIA_KEYS, mediaKeyEvent, type MediaKey } from '../lib/mediaKeys'
import type { PlayerEvent, PlayerState } from '../lib/player'
import { useWakeLock } from './useWakeLock'

export interface NowPlaying {
  title: string
  artist: string
  album: string
}

/**
 * Hands the player to the browser's Media Session, so the keyboard's media keys, Now Playing and
 * Bluetooth headset buttons drive it. The playback state stays "playing" through the silence and
 * waits between clips, when no audio element is playing.
 */
export function useMediaKeys(
  state: PlayerState,
  dispatch: (event: PlayerEvent) => void,
  startFrom: () => number | null,
  nowPlaying: NowPlaying | null,
) {
  const latest = useRef({ state, dispatch, startFrom })
  useEffect(() => {
    latest.current = { state, dispatch, startFrom }
  })

  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    const session = navigator.mediaSession
    const registered: MediaKey[] = []
    for (const key of MEDIA_KEYS) {
      try {
        session.setActionHandler(key, () => {
          const { state, dispatch, startFrom } = latest.current
          const event = mediaKeyEvent(state, key, startFrom())
          // Hidden pages may suspend scheduled React work after this callback.
          // Apply audio and Media Session changes while the system command is active.
          if (event) flushSync(() => dispatch(event))
        })
        registered.push(key)
      } catch (error) {
        if (!(error instanceof DOMException && error.name === 'NotSupportedError')) throw error
      }
    }
    return () => {
      for (const key of registered) session.setActionHandler(key, null)
      session.metadata = null
      session.playbackState = 'none'
    }
  }, [])

  const running = isRunning(state)
  useWakeLock(running)
  const playbackState = state.status === 'idle' ? 'none' : running ? 'playing' : 'paused'
  useEffect(() => {
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = playbackState
  }, [playbackState])

  const { title = '', artist = '', album = '' } = nowPlaying ?? {}
  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    navigator.mediaSession.metadata = title ? new MediaMetadata({ title, artist, album }) : null
  }, [title, artist, album])
}
