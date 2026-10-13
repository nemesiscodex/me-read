import { useEffect, useRef } from 'react'
import { isRunning, MEDIA_KEYS, mediaKeyEvent } from '../lib/mediaKeys'
import type { PlayerEvent, PlayerState } from '../lib/player'

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
    for (const key of MEDIA_KEYS) {
      session.setActionHandler(key, () => {
        const { state, dispatch, startFrom } = latest.current
        const event = mediaKeyEvent(state, key, startFrom())
        if (event) dispatch(event)
      })
    }
    return () => {
      for (const key of MEDIA_KEYS) session.setActionHandler(key, null)
      session.metadata = null
      session.playbackState = 'none'
    }
  }, [])

  const running = isRunning(state)
  useEffect(() => {
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = running ? 'playing' : 'paused'
  }, [running])

  const { title = '', artist = '', album = '' } = nowPlaying ?? {}
  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    navigator.mediaSession.metadata = title ? new MediaMetadata({ title, artist, album }) : null
  }, [title, artist, album])
}
