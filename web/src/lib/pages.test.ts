import { describe, expect, it } from 'vitest'
import {
  chapterContaining,
  focusFor,
  layoutFor,
  pageCountOf,
  pageForAnchor,
  pageOfOffset,
  type BlockSpan,
} from './pages'

const span = (idx: number, firstPage: number, lastPage = firstPage): BlockSpan => ({ idx, firstPage, lastPage })

// Chapter of blocks 100..106 over four pages; block 102 runs from page 0 onto page 1.
const spans = [span(100, 0), span(101, 0), span(102, 0, 1), span(103, 1), span(104, 2), span(105, 2, 3), span(106, 3)]
const chapter = { first_block: 100, block_count: 7 }

describe('layoutFor', () => {
  it('uses one column on a narrow stage and never exceeds the stage', () => {
    const layout = layoutFor(380, 600.7, 18)
    expect(layout).toMatchObject({ columns: 1, width: 380, height: 600 })
    expect(layout.stride).toBe(layout.width + layout.gap)
  })

  it('caps the line length on a wide single column', () => {
    expect(layoutFor(800, 600, 18)).toMatchObject({ columns: 1, width: 34 * 18 })
  })

  it('shows a two-page spread when both columns keep a comfortable measure', () => {
    const layout = layoutFor(1400, 800, 18)
    expect(layout.columns).toBe(2)
    expect(layout.width).toBeLessThanOrEqual(1400)
    expect((layout.width - layout.gap) % 2).toBe(0)
  })

  it('falls back to one column when the text is enlarged', () => {
    expect(layoutFor(1000, 800, 16).columns).toBe(2)
    expect(layoutFor(1000, 800, 26).columns).toBe(1)
  })
})

describe('pageOfOffset', () => {
  it('maps column positions to pages, tolerating sub-pixel drift', () => {
    expect(pageOfOffset(0, 500)).toBe(0)
    expect(pageOfOffset(260, 500)).toBe(0)
    expect(pageOfOffset(499.6, 500)).toBe(1)
    expect(pageOfOffset(1500.2, 500)).toBe(3)
    expect(pageOfOffset(-0.4, 500)).toBe(0)
  })
})

describe('focusFor', () => {
  it('spans the previous, current and next page in the middle of a chapter', () => {
    expect(focusFor(spans, 1, chapter, 1000)).toEqual({ first: 100, last: 105, current_first: 102, current_last: 103 })
  })

  it('reaches back into the previous chapter from the first page', () => {
    expect(focusFor(spans, 0, chapter, 1000)).toEqual({ first: 92, last: 103, current_first: 100, current_last: 102 })
  })

  it('reaches forward into the next chapter from the last page', () => {
    expect(focusFor(spans, 3, chapter, 1000)).toEqual({ first: 104, last: 114, current_first: 105, current_last: 106 })
  })

  it('stays inside the book at its very start and end', () => {
    const opening = [span(0, 0), span(1, 0)]
    expect(focusFor(opening, 0, { first_block: 0, block_count: 2 }, 2)).toEqual({
      first: 0,
      last: 1,
      current_first: 0,
      current_last: 1,
    })
    expect(focusFor(spans, 3, chapter, 109)?.last).toBe(108)
  })

  it('reaches both ways for a chapter that fits on one page', () => {
    expect(focusFor([span(100, 0)], 0, { first_block: 100, block_count: 1 }, 1000)).toEqual({
      first: 92,
      last: 108,
      current_first: 100,
      current_last: 100,
    })
  })

  it('has nothing to report for a page without text', () => {
    expect(focusFor([], 0, chapter, 1000)).toBeNull()
    expect(focusFor(spans, 9, chapter, 1000)).toBeNull()
  })
})

describe('pageForAnchor', () => {
  it('lands on the first or last page of a chapter', () => {
    expect(pageForAnchor({ kind: 'start' }, spans)).toBe(0)
    expect(pageForAnchor({ kind: 'end' }, spans)).toBe(3)
    expect(pageCountOf(spans)).toBe(4)
  })

  it('lands on the page where a block begins', () => {
    expect(pageForAnchor({ kind: 'block', idx: 102 }, spans)).toBe(0)
    expect(pageForAnchor({ kind: 'block', idx: 105 }, spans)).toBe(2)
  })

  it('falls back to the nearest end for a block outside the chapter', () => {
    expect(pageForAnchor({ kind: 'block', idx: 50 }, spans)).toBe(0)
    expect(pageForAnchor({ kind: 'block', idx: 500 }, spans)).toBe(3)
    expect(pageForAnchor({ kind: 'end' }, [])).toBe(0)
  })
})

describe('chapterContaining', () => {
  const chapters = [
    { index: 0, title: 'One', first_block: 0, block_count: 10 },
    { index: 1, title: 'Two', first_block: 10, block_count: 5 },
  ]

  it('finds the chapter by global block position', () => {
    expect(chapterContaining(chapters, 9)?.index).toBe(0)
    expect(chapterContaining(chapters, 10)?.index).toBe(1)
  })

  it('opens the first chapter for a position outside every chapter', () => {
    expect(chapterContaining(chapters, 99)?.index).toBe(0)
    expect(chapterContaining([], 0)).toBeNull()
  })
})
