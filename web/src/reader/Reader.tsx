import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type MouseEvent } from 'react'
import { api, type Character, type ServerEvent } from '../api'
import { TaggerSwitch } from './TaggerSwitch'
import { Icon } from '../Icon'
import { characterHue, narratorOf } from '../lib/cast'
import { chapterContaining, focusFor, layoutFor, type Anchor } from '../lib/pages'
import { activeSegmentId } from '../lib/player'
import { FONT_SIZE, SPEED, useNumberPref } from '../lib/prefs'
import { readerReducer } from '../lib/readerState'
import { loadPauses, pausesAfter, savePauses, type Pauses } from '../lib/pauses'
import { libraryHref } from '../lib/route'
import { indexSegments, isUntagged, playItems, segmentStatus, type SegmentStatus } from '../lib/segments'
import { BlockView, segmentIdAt, type ActiveMode } from './BlockView'
import { CastPanel } from './CastPanel'
import { CorrectionPopover } from './CorrectionPopover'
import { PlayerBar } from './PlayerBar'
import { SegmentHint, type Hint } from './SegmentHint'
import { usePagination } from './usePagination'
import { usePlayer } from './usePlayer'
import './reader.css'

const FOCUS_DEBOUNCE_MS = 300
const NOTICE_MS = 6000

const STATUS_NOTE: Record<SegmentStatus, string | null> = {
  untagged: 'waiting to find out who says this',
  tagging: 'finding out who says this…',
  'tag-failed': 'could not tell who says this, right-click to choose',
  waiting: 'waiting for audio',
  generating: 'recording…',
  ready: null,
  failed: 'audio failed',
}

function useElementSize() {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const ref = useCallback((el: HTMLElement | null) => {
    observer.current?.disconnect()
    if (!el) return
    observer.current = new ResizeObserver(([entry]) => {
      const { width, height } = entry!.contentRect
      setSize((prev) => (prev && prev.width === width && prev.height === height ? prev : { width, height }))
    })
    observer.current.observe(el)
  }, [])
  return [size, ref] as const
}

function isTyping(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
}

const messageOf = (error: unknown) => (error instanceof Error ? error.message : String(error))

export function Reader({ bookId }: { bookId: number }) {
  const [data, dispatch] = useReducer(readerReducer, null)
  const [openError, setOpenError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [offline, setOffline] = useState(false)
  const [castOpen, setCastOpen] = useState(false)
  const [correcting, setCorrecting] = useState<{ segmentId: number; x: number; y: number } | null>(null)
  const [fontPx, setFontPx] = useNumberPref(FONT_SIZE)
  const [rate, setRate] = useNumberPref(SPEED)
  const [well, wellRef] = useElementSize()

  const layout = useMemo(() => (well ? layoutFor(well.width, well.height, fontPx) : null), [well, fontPx])
  const pagination = usePagination(layout, data ? `${data.chapterIndex}:${data.loadSeq}` : null, fontPx)
  const { setAnchor, turnTo, showSegment, currentAnchor } = pagination

  const chapters = data?.book.chapters ?? []
  const chapterAt = data ? chapters.findIndex((c) => c.index === data.chapterIndex) : -1
  const chapter = chapters[chapterAt] ?? null
  const previousChapter = chapters[chapterAt - 1] ?? null
  const nextChapter = chapters[chapterAt + 1] ?? null

  const blocks = data?.blocks
  const segments = useMemo(() => indexSegments(blocks ?? []), [blocks])
  const items = useMemo(() => playItems(blocks ?? []), [blocks])
  const characters = useMemo(
    () => new Map((data?.book.characters ?? []).map((c): [number, Character] => [c.id, c])),
    [data?.book.characters],
  )
  const narratorId = useMemo(() => narratorOf(data?.book.characters ?? [])?.id ?? null, [data?.book.characters])

  const clipUrl = useCallback(
    (segmentId: number) => {
      const found = segments.get(segmentId)
      return found ? api.audioUrl(found.segment) : null
    },
    [segments],
  )
  const [pauseLengths, setPauseLengths] = useState<Pauses>(loadPauses)
  const pauses = useMemo(() => pausesAfter(blocks ?? [], pauseLengths), [blocks, pauseLengths])
  const player = usePlayer({ items, hasNextChapter: nextChapter !== null }, clipUrl, rate, pauses)
  const playerDispatch = player.dispatch
  const activeId = activeSegmentId(player.state)
  const active = activeId === null ? null : (segments.get(activeId) ?? null)

  useEffect(() => {
    if (notice === null) return
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS)
    return () => window.clearTimeout(timer)
  }, [notice])

  const requests = useRef(0)
  const loadChapter = useCallback(
    async (chapterIndex: number, anchor: Anchor | null): Promise<boolean> => {
      const request = ++requests.current
      try {
        const loaded = await api.getChapter(bookId, chapterIndex)
        if (request !== requests.current) return false
        if (anchor) setAnchor(anchor)
        dispatch({ type: 'chapterLoaded', chapterIndex, blocks: loaded })
        return true
      } catch (error) {
        if (request === requests.current) setNotice(`Could not load the chapter. ${messageOf(error)}`)
        return false
      }
    },
    [bookId, setAnchor],
  )

  useEffect(() => {
    let alive = true
    const open = async () => {
      const book = await api.getBook(bookId)
      const first = chapterContaining(book.chapters, book.position_block)
      if (!first) throw new Error('This book has no chapters.')
      const loaded = await api.getChapter(bookId, first.index)
      if (!alive) return
      setAnchor({ kind: 'block', idx: book.position_block })
      dispatch({ type: 'opened', book, chapterIndex: first.index, blocks: loaded })
    }
    open().catch((error) => alive && setOpenError(messageOf(error)))
    return () => {
      alive = false
    }
  }, [bookId, setAnchor])

  useEffect(() => {
    const source = api.events(bookId)
    let dropped = false
    source.onmessage = (message) => {
      let event: ServerEvent
      try {
        event = JSON.parse(message.data)
      } catch {
        return
      }
      dispatch({ type: 'event', event })
    }
    source.onerror = () => {
      dropped = true
      setOffline(true)
    }
    source.onopen = () => {
      setOffline(false)
      if (!dropped) return
      dropped = false
      api.getBook(bookId).then((book) => dispatch({ type: 'bookRefreshed', book }), () => {})
      const current = chapterIndexRef.current.current
      if (current !== null) void loadChapter(current, null)
    }
    return () => source.close()
  }, [bookId, loadChapter])

  useEffect(() => {
    playerDispatch({ type: 'sync' })
  }, [items, playerDispatch])

  const loadSeq = data?.loadSeq ?? 0
  useEffect(() => {
    if (loadSeq > 0) playerDispatch({ type: 'chapterLoaded' })
  }, [loadSeq, playerDispatch])

  const chapterIndexRef = useRef({ current: null as number | null, next: null as number | null })
  useEffect(() => {
    chapterIndexRef.current = { current: data?.chapterIndex ?? null, next: nextChapter?.index ?? null }
  })

  const turning = player.state.status === 'turning'
  useEffect(() => {
    if (!turning) return
    const next = chapterIndexRef.current.next
    if (next === null) return playerDispatch({ type: 'stop' })
    let alive = true
    void loadChapter(next, { kind: 'start' }).then((loaded) => {
      if (alive && !loaded) playerDispatch({ type: 'stop' })
    })
    return () => {
      alive = false
    }
  }, [turning, player.state.run, loadChapter, playerDispatch])

  useEffect(() => {
    if (activeId !== null) showSegment(activeId)
  }, [activeId, loadSeq, showSegment])

  const bookBlockCount = data?.book.block_count ?? 0
  const focus = useMemo(
    () => (chapter ? focusFor(pagination.spans, pagination.page, chapter, bookBlockCount) : null),
    [pagination.spans, pagination.page, chapter, bookBlockCount],
  )
  const focusKey = focus ? `${focus.first}:${focus.last}:${focus.current_first}:${focus.current_last}` : null
  const focusRef = useRef(focus)
  useEffect(() => {
    focusRef.current = focus
  })
  useEffect(() => {
    if (focusKey === null) return
    const timer = window.setTimeout(() => {
      if (focusRef.current) api.setFocus(bookId, focusRef.current).catch(() => {})
    }, FOCUS_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [bookId, focusKey])

  const turn = useCallback(
    (dir: 1 | -1) => {
      const wanted = pagination.page + dir
      if (wanted >= 0 && wanted < pagination.pageCount) return turnTo(wanted)
      if (dir === 1 && nextChapter) void loadChapter(nextChapter.index, { kind: 'start' })
      if (dir === -1 && previousChapter) void loadChapter(previousChapter.index, { kind: 'end' })
    },
    [pagination.page, pagination.pageCount, turnTo, nextChapter, previousChapter, loadChapter],
  )

  const startFrom = useCallback((): number | null => {
    const anchor = currentAnchor()
    if (anchor.kind === 'segment') return anchor.id
    if (anchor.kind === 'block') return blocks?.find((b) => b.idx === anchor.idx)?.segments[0]?.id ?? null
    return blocks?.[0]?.segments[0]?.id ?? null
  }, [currentAnchor, blocks])

  const toggle = useCallback(() => playerDispatch({ type: 'toggle', from: startFrom() }), [playerDispatch, startFrom])

  const keys = useRef({ turn, toggle })
  useEffect(() => {
    keys.current = { turn, toggle }
  })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return
      if (event.key === 'ArrowRight' || event.key === 'PageDown') keys.current.turn(1)
      else if (event.key === 'ArrowLeft' || event.key === 'PageUp') keys.current.turn(-1)
      else if (event.key === ' ' && !(event.target instanceof HTMLButtonElement)) keys.current.toggle()
      else return
      event.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const onPageClick = (event: MouseEvent) => {
    const segmentId = segmentIdAt(event.target)
    if (segmentId === null) return
    if (event.altKey) return setCorrecting({ segmentId, x: event.clientX, y: event.clientY })
    if (window.getSelection()?.isCollapsed === false) return
    playerDispatch({ type: 'play', segmentId })
  }

  const onPageContextMenu = (event: MouseEvent) => {
    const segmentId = segmentIdAt(event.target)
    if (segmentId === null) return
    event.preventDefault()
    setCorrecting({ segmentId, x: event.clientX, y: event.clientY })
  }

  const describe = useCallback(
    (segmentId: number): Hint | null => {
      const found = segments.get(segmentId)
      if (!found) return null
      const { segment, block } = found
      const status = segmentStatus(segment, block.tag_state)
      const character = segment.character_id === null ? null : (characters.get(segment.character_id) ?? null)
      const voiced = character !== null && character.role !== 'narrator'
      if (status === 'ready' && !voiced) return null
      return {
        speaker: character?.name ?? (isUntagged(segment) ? 'Speaker unknown' : 'Narration'),
        hue: voiced ? characterHue(character.id) : null,
        note: status === 'failed' ? (segment.error ?? STATUS_NOTE.failed) : STATUS_NOTE[status],
        problem: status === 'failed' || status === 'tag-failed',
      }
    },
    [segments, characters],
  )

  const closeCorrection = useCallback(() => setCorrecting(null), [])
  const closeCast = useCallback(() => setCastOpen(false), [])
  const pauseForSample = useCallback(() => {
    if (player.state.status === 'playing' || player.state.status === 'waiting') {
      playerDispatch({ type: 'toggle', from: null })
    }
  }, [player.state.status, playerDispatch])

  if (openError) {
    return (
      <div className="reader-message">
        <p>Could not open this book. {openError}</p>
        <a href={libraryHref}>Back to the library</a>
      </div>
    )
  }
  if (!data || !chapter) return <div className="reader-message">Opening the book…</div>

  const activeMode: ActiveMode | null =
    player.state.status === 'playing' || player.state.status === 'paused' || player.state.status === 'waiting'
      ? player.state.status
      : null
  const correctingTarget = correcting ? segments.get(correcting.segmentId) : null
  const speaker =
    active && active.segment.character_id !== null ? (characters.get(active.segment.character_id) ?? null) : null
  const activity = [data.activity.tagger, data.activity.speech].filter((line) => line !== null && line !== '')
  const atStart = pagination.page === 0 && !previousChapter
  const atEnd = pagination.page >= pagination.pageCount - 1 && !nextChapter

  return (
    <div className="reader">
      <header className="reader-top">
        <a className="back-link" href={libraryHref}>
          <Icon name="left" size={16} />
          Library
        </a>
        <div className="reader-place">
          <span className="reader-title">{data.book.title}</span>
          <select
            className="chapter-select"
            aria-label="Chapter"
            value={chapter.index}
            onChange={(event) => void loadChapter(Number(event.target.value), { kind: 'start' })}
          >
            {chapters.map((c, at) => (
              <option key={c.index} value={c.index}>
                {c.title.trim() || `Chapter ${at + 1}`}
              </option>
            ))}
          </select>
        </div>
        <div className="reader-tools">
          <button
            className="quiet-button type-smaller"
            onClick={() => setFontPx(fontPx - 1)}
            disabled={fontPx <= FONT_SIZE.min}
            aria-label="Smaller text"
          >
            A
          </button>
          <button
            className="quiet-button type-larger"
            onClick={() => setFontPx(fontPx + 1)}
            disabled={fontPx >= FONT_SIZE.max}
            aria-label="Larger text"
          >
            A
          </button>
          <TaggerSwitch activity={data.activity.tagger} />
          <button className="quiet-button" onClick={() => setCastOpen((open) => !open)} aria-expanded={castOpen}>
            Cast
          </button>
        </div>
      </header>

      <main className="stage">
        <button className="edge" onClick={() => turn(-1)} disabled={atStart} aria-label="Previous page">
          <Icon name="left" size={22} />
        </button>
        <div className="well" ref={wellRef}>
          {layout && (
            <div className="viewport" style={{ width: layout.width, height: layout.height }}>
              <div
                className="columns"
                ref={pagination.columnsRef}
                data-animate={pagination.animate || undefined}
                onClick={onPageClick}
                onContextMenu={onPageContextMenu}
                style={{
                  height: layout.height,
                  fontSize: fontPx,
                  columnCount: layout.columns,
                  columnGap: layout.gap,
                  transform: `translateX(${-pagination.page * layout.stride}px)`,
                }}
              >
                {data.blocks.map((block) => {
                  const holdsActive = active?.block.id === block.id
                  return (
                    <BlockView
                      key={block.id}
                      block={block}
                      narratorId={narratorId}
                      activeId={holdsActive ? activeId : null}
                      activeMode={holdsActive ? activeMode : null}
                    />
                  )
                })}
              </div>
            </div>
          )}
        </div>
        <button className="edge" onClick={() => turn(1)} disabled={atEnd} aria-label="Next page">
          <Icon name="right" size={22} />
        </button>
      </main>

      <footer className="reader-bottom">
        <PlayerBar
          state={player.state}
          speaker={speaker}
          canStart={items.length > 0}
          rate={rate}
          pauses={pauseLengths}
          onPauses={(next) => {
            setPauseLengths(next)
            savePauses(next)
          }}
          onToggle={toggle}
          onSkip={(dir) => playerDispatch({ type: 'skip', dir })}
          onRate={setRate}
          onCorrect={(anchor) => {
            if (activeId === null) return
            const box = anchor.getBoundingClientRect()
            setCorrecting({ segmentId: activeId, x: box.left, y: box.top - 8 })
          }}
        />
        <p className="status-line" data-problem={notice !== null || offline || undefined} aria-live="polite">
          {notice ?? (offline ? 'Lost the connection to the server. Trying again…' : activity.join('  /  '))}
        </p>
        <div className="page-count">
          <button className="icon-button" onClick={() => turn(-1)} disabled={atStart} aria-label="Previous page">
            <Icon name="left" size={16} />
          </button>
          <span>
            {pagination.page + 1} of {pagination.pageCount}
          </span>
          <button className="icon-button" onClick={() => turn(1)} disabled={atEnd} aria-label="Next page">
            <Icon name="right" size={16} />
          </button>
        </div>
      </footer>

      <SegmentHint key={`${loadSeq}:${pagination.page}`} describe={describe} />

      {correcting && correctingTarget && (
        <CorrectionPopover
          bookId={bookId}
          segment={correctingTarget.segment}
          block={correctingTarget.block}
          characters={data.book.characters}
          at={correcting}
          onClose={closeCorrection}
          onSegment={(segment) => dispatch({ type: 'segmentSaved', segment })}
          onCharacter={(character) => dispatch({ type: 'characterSaved', character })}
        />
      )}

      {castOpen && (
        <CastPanel
          characters={data.book.characters}
          onClose={closeCast}
          onCharacter={(character) => dispatch({ type: 'characterSaved', character })}
          onMerged={(id, into) => dispatch({ type: 'characterMerged', id, into })}
          onSample={pauseForSample}
        />
      )}
    </div>
  )
}
