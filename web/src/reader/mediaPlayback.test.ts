// @vitest-environment happy-dom
import { act, createElement, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PlayerContext } from '../lib/player'
import { useMediaKeys } from './useMediaKeys'
import { usePlayer, type Player } from './usePlayer'

let root: Root
let player: Player
let ctx: PlayerContext
let rate: number
let visible: boolean
let handlers: Map<MediaSessionAction, MediaSessionActionHandler>
let locks: ReturnType<typeof makeLock>[]
let request: ReturnType<typeof vi.fn<WakeLock['request']>>
let play: ReturnType<typeof vi.fn<HTMLMediaElement['play']>>
let played: HTMLMediaElement[]

function makeLock() {
  return new class extends EventTarget implements WakeLockSentinel {
    readonly type = 'screen'
    onrelease: WakeLockSentinel['onrelease'] = null
    released = false
    release = vi.fn(async () => {
      this.released = true
      this.dispatchEvent(new Event('release'))
    })
  }()
}

function Harness() {
  player = usePlayer(ctx, (id) => `/audio/${id}.wav`, rate, new Map([[1, 0.4]]))
  useMediaKeys(player.state, player.dispatch, () => 1, { title: 'Chapter 1', artist: 'Author', album: 'Book' })
  return null
}

async function render() {
  await act(async () => root.render(createElement(StrictMode, null, createElement(Harness))))
}

async function send(action: MediaSessionAction) {
  const handler = handlers.get(action)
  expect(handler).toBeDefined()
  await act(async () => handler?.({ action }))
}

async function visibility(value: boolean) {
  visible = value
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.useFakeTimers()
  ctx = { items: [{ id: 1, playability: 'ready' }, { id: 2, playability: 'ready' }], hasNextChapter: false }
  rate = 1
  visible = true
  handlers = new Map()
  locks = []
  request = vi.fn<WakeLock['request']>(async () => {
    const lock = makeLock()
    locks.push(lock)
    return lock
  })
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visible ? 'visible' : 'hidden')
  Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: { request } })
  Object.defineProperty(navigator, 'mediaSession', { configurable: true, value: {
    playbackState: 'none', metadata: null,
    setActionHandler: (action: MediaSessionAction, handler: MediaSessionActionHandler | null) => {
      if (handler) handlers.set(action, handler)
      else handlers.delete(action)
    },
  } })
  vi.stubGlobal('MediaMetadata', class { constructor(data: MediaMetadataInit) { Object.assign(this, data) } })
  played = []
  play = vi.fn(function (this: HTMLMediaElement) {
    played.push(this)
    return Promise.resolve()
  })
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(play)
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  root = createRoot(document.createElement('div'))
})

afterEach(async () => {
  await act(async () => root.unmount())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  Reflect.deleteProperty(navigator, 'wakeLock')
  Reflect.deleteProperty(navigator, 'mediaSession')
})

describe('reader media playback', () => {
  it('keeps the screen awake through line gaps and waiting, and releases it on pause and exit', async () => {
    await render()
    expect(request).not.toHaveBeenCalled()
    await send('play')
    expect(request).toHaveBeenCalledWith('screen')
    const lock = locks.at(-1)!
    const audio = played.at(-1)!
    await act(async () => audio.dispatchEvent(new Event('ended')))
    expect(lock.release).not.toHaveBeenCalled()
    ctx.items[1] = { id: 2, playability: 'pending' }
    await render()
    await act(async () => vi.advanceTimersByTime(400))
    expect(player.state.status).toBe('waiting')
    expect(lock.release).not.toHaveBeenCalled()
    await send('pause')
    expect(lock.release).toHaveBeenCalledOnce()
    await send('play')
    const resumed = locks.at(-1)!
    await act(async () => root.unmount())
    expect(resumed.release).toHaveBeenCalledOnce()
  })

  it('reacquires the screen lock when returning to the tab, but not while paused', async () => {
    await render()
    await send('play')
    await visibility(false)
    await act(async () => locks.at(-1)!.release())
    const count = request.mock.calls.length
    await visibility(true)
    expect(request).toHaveBeenCalledTimes(count + 1)
    await send('pause')
    await visibility(false)
    await visibility(true)
    expect(request).toHaveBeenCalledTimes(count + 1)
  })

  it('releases a lock that arrives after playback was paused', async () => {
    const lock = makeLock()
    let resolve: (lock: WakeLockSentinel) => void = () => {}
    request.mockImplementation(() => new Promise((done) => { resolve = done }))
    await render()
    await send('play')
    await send('pause')
    await act(async () => resolve(lock))
    expect(lock.release).toHaveBeenCalledOnce()
  })

  it('still plays and responds to media controls when screen locking is denied', async () => {
    request.mockRejectedValue(new DOMException('Battery saver', 'NotAllowedError'))
    await render()
    await send('play')
    expect(player.state.status).toBe('playing')
    expect(navigator.mediaSession.playbackState).toBe('playing')
    await send('pause')
    expect(player.state.status).toBe('paused')
    expect(navigator.mediaSession.playbackState).toBe('paused')
  })

  it('still plays without wake lock or media session APIs', async () => {
    Reflect.deleteProperty(navigator, 'wakeLock')
    Reflect.deleteProperty(navigator, 'mediaSession')
    await render()
    await act(async () => player.dispatch({ type: 'toggle', from: 1 }))
    expect(player.state.status).toBe('playing')
    await act(async () => player.dispatch({ type: 'toggle', from: 1 }))
    expect(player.state.status).toBe('paused')
  })

  it('registers the remaining media controls when a browser does not support stop', async () => {
    const set = navigator.mediaSession.setActionHandler
    vi.spyOn(navigator.mediaSession, 'setActionHandler').mockImplementation((action, handler) => {
      if (action === 'stop') throw new DOMException('Unsupported action', 'NotSupportedError')
      set(action, handler)
    })
    await render()
    await send('play')
    await send('nexttrack')
    expect(player.state).toMatchObject({ status: 'playing', segmentId: 2 })
    await send('previoustrack')
    expect(player.state).toMatchObject({ status: 'playing', segmentId: 1 })
    await send('pause')
    await send('play')
    expect(player.state.status).toBe('playing')
  })

  it('continues on the audio element the user started, including after buffering and chapter changes', async () => {
    const permitted = new WeakSet<HTMLMediaElement>()
    let userGesture = true
    play.mockImplementation(function (this: HTMLMediaElement) {
      played.push(this)
      if (userGesture) permitted.add(this)
      return permitted.has(this) ? Promise.resolve() : Promise.reject(new DOMException('Gesture required', 'NotAllowedError'))
    })
    await render()
    await send('play')
    userGesture = false
    const audio = played.at(-1)!
    await act(async () => audio.dispatchEvent(new Event('ended')))
    await act(async () => vi.advanceTimersByTime(400))
    expect(player.state).toMatchObject({ status: 'playing', segmentId: 2 })
    expect(played.at(-1)).toBe(audio)
    await send('pause')
    audio.currentTime = 1.25
    await send('play')
    expect(audio.currentTime).toBe(1.25)
    rate = 1.5
    await render()
    expect(audio.playbackRate).toBe(1.5)
    ctx.hasNextChapter = true
    await render()
    await act(async () => audio.dispatchEvent(new Event('ended')))
    await act(async () => vi.runOnlyPendingTimers())
    expect(player.state.status).toBe('turning')
    ctx = { items: [{ id: 3, playability: 'pending' }], hasNextChapter: false }
    await render()
    await act(async () => player.dispatch({ type: 'chapterLoaded' }))
    expect(player.state.status).toBe('waiting')
    ctx.items[0] = { id: 3, playability: 'ready' }
    await render()
    await act(async () => player.dispatch({ type: 'sync' }))
    expect(player.state).toMatchObject({ status: 'playing', segmentId: 3 })
    expect(played.at(-1)).toBe(audio)
    await act(async () => audio.dispatchEvent(new Event('ended')))
    await act(async () => vi.runOnlyPendingTimers())
    expect(player.state.status).toBe('idle')
    expect(navigator.mediaSession.playbackState).toBe('none')
    expect(locks.every((lock) => lock.released)).toBe(true)
  })
})
