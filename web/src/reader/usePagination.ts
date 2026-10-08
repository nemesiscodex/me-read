import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import {
  pageCountOf,
  pageForAnchor,
  pageOfOffset,
  type Anchor,
  type BlockSpan,
  type PageLayout,
} from '../lib/pages'

export interface Pagination {
  columnsRef: RefObject<HTMLDivElement | null>
  page: number
  pageCount: number
  /** False when the page changed because the text was laid out again, so it should not slide. */
  animate: boolean
  spans: BlockSpan[]
  /** Where to land the next time the text is laid out. */
  setAnchor: (anchor: Anchor) => void
  currentAnchor: () => Anchor
  turnTo: (page: number) => void
  showSegment: (segmentId: number) => void
}

function firstRect(el: Element): DOMRect | null {
  for (const child of el.children) {
    const rect = child.getClientRects()[0]
    if (rect) return rect
  }
  return el.getClientRects()[0] ?? null
}

function lastRect(el: Element): DOMRect | null {
  for (let i = el.children.length - 1; i >= 0; i--) {
    const rects = el.children[i]!.getClientRects()
    if (rects.length > 0) return rects[rects.length - 1]!
  }
  const rects = el.getClientRects()
  return rects[rects.length - 1] ?? null
}

function measureBlocks(root: HTMLElement, stride: number): BlockSpan[] {
  const origin = root.getBoundingClientRect().left
  const spans: BlockSpan[] = []
  for (const el of root.querySelectorAll<HTMLElement>('[data-block]')) {
    const first = firstRect(el)
    const last = lastRect(el)
    if (!first || !last) continue
    const firstPage = pageOfOffset(first.left - origin, stride)
    spans.push({
      idx: Number(el.dataset.block),
      firstPage,
      lastPage: Math.max(firstPage, pageOfOffset(last.left - origin, stride)),
    })
  }
  return spans
}

function segmentPage(root: HTMLElement, stride: number, segmentId: number): number | null {
  const rect = root.querySelector(`[data-seg="${segmentId}"]`)?.getClientRects()[0]
  return rect ? pageOfOffset(rect.left - root.getBoundingClientRect().left, stride) : null
}

/** The first segment that begins on `page`, or failing that the first block that reaches it. */
function anchorAtTop(root: HTMLElement, stride: number, spans: BlockSpan[], page: number): Anchor | null {
  const origin = root.getBoundingClientRect().left
  const onPage = spans.filter((span) => span.firstPage <= page && span.lastPage >= page)
  for (const span of onPage) {
    const el = root.querySelector(`[data-block="${span.idx}"]`)
    for (const child of el?.children ?? []) {
      const rect = child.getClientRects()[0]
      const id = (child as HTMLElement).dataset.seg
      if (rect && id && pageOfOffset(rect.left - origin, stride) === page) return { kind: 'segment', id: Number(id) }
    }
  }
  return onPage[0] ? { kind: 'block', idx: onPage[0].idx } : null
}

const NO_SPANS: BlockSpan[] = []

export function usePagination(layout: PageLayout | null, contentKey: string | null, fontPx: number): Pagination {
  const columnsRef = useRef<HTMLDivElement>(null)
  const anchorRef = useRef<Anchor>({ kind: 'start' })
  const [state, setState] = useState({ spans: NO_SPANS, page: 0, animate: false })
  const live = useRef({ spans: NO_SPANS, page: 0, stride: 0 })

  useLayoutEffect(() => {
    const root = columnsRef.current
    if (!root || !layout || contentKey === null) return
    const spans = measureBlocks(root, layout.stride)
    const anchor = anchorRef.current
    const wanted =
      anchor.kind === 'segment'
        ? (segmentPage(root, layout.stride, anchor.id) ?? live.current.page)
        : pageForAnchor(anchor, spans)
    const page = Math.min(Math.max(0, wanted), pageCountOf(spans) - 1)
    anchorRef.current = anchorAtTop(root, layout.stride, spans, page) ?? anchor
    live.current = { spans, page, stride: layout.stride }
    setState({ spans, page, animate: false })
  }, [layout, contentKey, fontPx])

  const setAnchor = useCallback((anchor: Anchor) => {
    anchorRef.current = anchor
  }, [])

  const currentAnchor = useCallback(() => anchorRef.current, [])

  const turnTo = useCallback((wanted: number) => {
    const root = columnsRef.current
    const { spans, stride } = live.current
    if (!root || stride === 0) return
    const page = Math.min(Math.max(0, wanted), pageCountOf(spans) - 1)
    if (page === live.current.page) return
    anchorRef.current = anchorAtTop(root, stride, spans, page) ?? anchorRef.current
    live.current = { ...live.current, page }
    setState({ spans, page, animate: true })
  }, [])

  const showSegment = useCallback(
    (segmentId: number) => {
      const root = columnsRef.current
      if (!root || live.current.stride === 0) return
      const page = segmentPage(root, live.current.stride, segmentId)
      if (page !== null) turnTo(page)
    },
    [turnTo],
  )

  return {
    columnsRef,
    page: state.page,
    pageCount: pageCountOf(state.spans),
    animate: state.animate,
    spans: state.spans,
    setAnchor,
    currentAnchor,
    turnTo,
    showSegment,
  }
}
