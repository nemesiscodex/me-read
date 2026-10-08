import { useCallback, useEffect, useRef, useState } from 'react'
import { initialPlayer, step, type PlayerContext, type PlayerEvent, type PlayerState } from '../lib/player'
import { nextPlayable } from '../lib/segments'

interface LoadedClip {
  segmentId: number
  run: number
  el: HTMLAudioElement
}

export interface Player {
  state: PlayerState
  dispatch: (event: PlayerEvent) => void
}

function release(el: HTMLAudioElement) {
  el.onended = null
  el.onerror = null
  el.pause()
}

/**
 * Drives audio elements from the player state machine. `clipUrl` is read only when a clip starts,
 * so a clip regenerated while it plays is left alone and picked up on its next start.
 */
export function usePlayer(
  ctx: PlayerContext,
  clipUrl: (segmentId: number) => string | null,
  rate: number,
  /** Seconds of silence to leave after each segment at normal speed. */
  pauses: Map<number, number>,
): Player {
  const [state, setState] = useState<PlayerState>(initialPlayer)
  const ctxRef = useRef(ctx)
  const urlRef = useRef(clipUrl)
  const rateRef = useRef(rate)
  const pausesRef = useRef(pauses)
  const rest = useRef<number | undefined>(undefined)
  const current = useRef<LoadedClip | null>(null)
  const preloaded = useRef<{ url: string; el: HTMLAudioElement } | null>(null)

  useEffect(() => {
    ctxRef.current = ctx
    urlRef.current = clipUrl
    rateRef.current = rate
    pausesRef.current = pauses
  })

  const dispatch = useCallback((event: PlayerEvent) => {
    setState((prev) => step(prev, event, ctxRef.current))
  }, [])

  const playingId = state.status === 'playing' ? state.segmentId : null
  const heldId = state.status === 'paused' ? state.segmentId : playingId

  useEffect(() => {
    window.clearTimeout(rest.current)
    const loaded = current.current
    if (playingId === null) {
      loaded?.el.pause()
      if (loaded && loaded.segmentId !== heldId) {
        release(loaded.el)
        current.current = null
      }
      return
    }

    const event = { segmentId: playingId, run: state.run }
    let el: HTMLAudioElement
    if (loaded && loaded.segmentId === playingId && loaded.run === state.run) {
      el = loaded.el
    } else {
      if (loaded) release(loaded.el)
      current.current = null
      const url = urlRef.current(playingId)
      if (url === null) {
        dispatch({ type: 'clipFailed', ...event })
        return
      }
      const ready = preloaded.current
      if (ready && ready.url === url) {
        el = ready.el
        preloaded.current = null
      } else {
        el = new Audio(url)
      }
      current.current = { ...event, el }
    }

    el.defaultPlaybackRate = rateRef.current
    el.playbackRate = rateRef.current
    // Paused during the silence after the line: resuming moves on instead of replaying it.
    if (el.ended) {
      dispatch({ type: 'ended', ...event })
      return
    }
    el.onended = () => {
      const seconds = (pausesRef.current.get(event.segmentId) ?? 0) / rateRef.current
      rest.current = window.setTimeout(() => dispatch({ type: 'ended', ...event }), seconds * 1000)
    }
    el.onerror = () => dispatch({ type: 'clipFailed', ...event })
    el.play().catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return
      dispatch({ type: 'clipFailed', ...event })
    })
  }, [playingId, heldId, state.run, dispatch])

  useEffect(() => {
    const el = current.current?.el
    if (!el) return
    el.defaultPlaybackRate = rate
    el.playbackRate = rate
  }, [rate])

  const upcoming = playingId === null ? null : nextPlayable(ctx.items, playingId)
  const upcomingUrl = upcoming?.playability === 'ready' ? clipUrl(upcoming.id) : null

  useEffect(() => {
    if (upcomingUrl === null || preloaded.current?.url === upcomingUrl) return
    const el = new Audio()
    el.preload = 'auto'
    el.src = upcomingUrl
    preloaded.current = { url: upcomingUrl, el }
  }, [upcomingUrl])

  useEffect(
    () => () => {
      window.clearTimeout(rest.current)
      if (current.current) release(current.current.el)
      current.current = null
      preloaded.current = null
    },
    [],
  )

  return { state, dispatch }
}
