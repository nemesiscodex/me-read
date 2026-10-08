import type { Chapter, Focus } from '../api'

/** One screenful: one or two text columns of equal width. */
export interface PageLayout {
  columns: 1 | 2
  width: number
  height: number
  gap: number
  /** Horizontal distance from one page to the next. */
  stride: number
}

/** The pages a block touches, measured from where its text was laid out. */
export interface BlockSpan {
  idx: number
  firstPage: number
  lastPage: number
}

/** What the reader keeps in view when the layout or the chapter changes. */
export type Anchor = { kind: 'start' } | { kind: 'end' } | { kind: 'block'; idx: number } | { kind: 'segment'; id: number }

const MAX_MEASURE_EM = 34
const MIN_SPREAD_MEASURE_EM = 24
const GAP_EM = 3

export function layoutFor(availableWidth: number, availableHeight: number, fontPx: number): PageLayout {
  const gap = Math.round(GAP_EM * fontPx)
  const columns = availableWidth >= 2 * MIN_SPREAD_MEASURE_EM * fontPx + gap ? 2 : 1
  const widest = columns * MAX_MEASURE_EM * fontPx + (columns - 1) * gap
  let width = Math.max(1, Math.floor(Math.min(availableWidth, widest)))
  if (columns === 2 && (width - gap) % 2 !== 0) width -= 1
  return { columns, width, height: Math.max(1, Math.floor(availableHeight)), gap, stride: width + gap }
}

/** The page holding a point `offset` pixels from the left edge of the first page. */
export function pageOfOffset(offset: number, stride: number): number {
  return Math.max(0, Math.floor((offset + 1) / stride))
}

export function pageCountOf(spans: BlockSpan[]): number {
  return spans.reduce((count, span) => Math.max(count, span.lastPage + 1), 1)
}

function blocksOn(spans: BlockSpan[], page: number): { first: number; last: number } | null {
  let range: { first: number; last: number } | null = null
  for (const span of spans) {
    if (span.firstPage > page || span.lastPage < page) continue
    range = range
      ? { first: Math.min(range.first, span.idx), last: Math.max(range.last, span.idx) }
      : { first: span.idx, last: span.idx }
  }
  return range
}

export const CHAPTER_SPILL = 8

/**
 * Block range for the previous, current and next page. At a chapter edge the neighbouring page
 * belongs to another chapter that is not laid out, so the range reaches `spill` blocks into it.
 */
export function focusFor(
  spans: BlockSpan[],
  page: number,
  chapter: Pick<Chapter, 'first_block' | 'block_count'>,
  bookBlockCount: number,
  spill = CHAPTER_SPILL,
): Focus | null {
  const current = blocksOn(spans, page)
  if (!current) return null
  const chapterLast = chapter.first_block + chapter.block_count - 1
  const lastPage = pageCountOf(spans) - 1
  const before = page <= 0 ? chapter.first_block - spill : (blocksOn(spans, page - 1)?.first ?? current.first)
  const after = page >= lastPage ? chapterLast + spill : (blocksOn(spans, page + 1)?.last ?? current.last)
  return {
    first: Math.max(0, Math.min(before, current.first)),
    last: Math.max(current.last, Math.min(after, bookBlockCount - 1)),
    current_first: current.first,
    current_last: current.last,
  }
}

/** Page for an anchor that needs no DOM lookup. Segment anchors are resolved by the caller. */
export function pageForAnchor(anchor: Exclude<Anchor, { kind: 'segment' }>, spans: BlockSpan[]): number {
  switch (anchor.kind) {
    case 'start':
      return 0
    case 'end':
      return pageCountOf(spans) - 1
    case 'block': {
      const exact = spans.find((span) => span.idx === anchor.idx)
      if (exact) return exact.firstPage
      const first = spans[0]
      return first && anchor.idx > first.idx ? pageCountOf(spans) - 1 : 0
    }
  }
}

export function chapterContaining(chapters: Chapter[], blockIdx: number): Chapter | null {
  return (
    chapters.find((c) => blockIdx >= c.first_block && blockIdx < c.first_block + c.block_count) ?? chapters[0] ?? null
  )
}
