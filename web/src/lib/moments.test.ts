import { describe, expect, it } from 'vitest'
import { moment } from './fixtures'
import { countedMoments, illustrating, momentReached, momentToShow, readingPosition } from './moments'
import type { BlockSpan } from './pages'

const span = (idx: number, firstPage: number, lastPage = firstPage): BlockSpan => ({ idx, firstPage, lastPage })

// Three pages: blocks 10..13 on page 0, 13 runs over onto page 1 with 14..16, and 17..18 on page 2.
const spans = [span(10, 0), span(11, 0), span(12, 0), span(13, 0, 1), span(14, 1), span(15, 1), span(16, 1), span(17, 2), span(18, 2)]
const shown = (moments: Parameters<typeof momentToShow>[0], position: number) => momentToShow(moments, spans, 2, position)?.id ?? null

describe('readingPosition', () => {
  it('is the paragraph being read while the player has a line', () => {
    expect(readingPosition(spans, 1, 15)).toBe(15)
    expect(readingPosition(spans, 0, 13)).toBe(13)
  })

  it('is the last paragraph before the page in view when nothing is being read', () => {
    expect(readingPosition(spans, 0, null)).toBe(9)
    expect(readingPosition(spans, 1, null)).toBe(12)
    expect(readingPosition(spans, 2, null)).toBe(16)
  })

  it('reaches nothing before the page is laid out', () => {
    expect(readingPosition([], 0, null)).toBe(-1)
  })
})

describe('momentToShow', () => {
  it('never shows a moment the reading has not reached', () => {
    const moments = [moment(11), moment(15)]
    expect(shown(moments, 10)).toBeNull()
    expect(shown(moments, 11)).toBe(11)
    expect(shown(moments, 14)).toBe(11)
    expect(shown(moments, 15)).toBe(15)
  })

  it('keeps a moment on the page in view hidden until it is read aloud or the page is turned', () => {
    const moments = [moment(11), moment(15)]
    expect(shown(moments, readingPosition(spans, 0, null))).toBeNull()
    expect(shown(moments, readingPosition(spans, 0, 11))).toBe(11)
    expect(shown(moments, readingPosition(spans, 1, null))).toBe(11)
    expect(shown(moments, readingPosition(spans, 2, null))).toBe(15)
  })

  it('keeps the last picture on later pages until the next one', () => {
    expect(shown([moment(10)], 18)).toBe(10)
  })

  it('counts only the first two pictures of a page', () => {
    const moments = [moment(16), moment(14), moment(15), moment(17)]
    expect(countedMoments(moments, spans, 2).map((m) => m.id)).toEqual([14, 15, 17])
    expect(shown(moments, 16)).toBe(15)
    expect(shown(moments, 17)).toBe(17)
  })

  it('counts as many pictures of a page as the reader set', () => {
    const moments = [moment(14), moment(15), moment(16), moment(17)]
    expect(countedMoments(moments, spans, 1).map((m) => m.id)).toEqual([14, 17])
    expect(countedMoments(moments, spans, 3).map((m) => m.id)).toEqual([14, 15, 16, 17])
    expect(momentToShow(moments, spans, 1, 16)?.id).toBe(14)
    expect(momentToShow(moments, spans, 3, 16)?.id).toBe(16)
    expect(momentReached([moment(14), moment(15, { image_state: 'drawing' })], spans, 1, 16)?.id).toBe(14)
  })

  it('counts the most important pictures of a page, the earlier one first when two matter as much', () => {
    const weighed = [moment(10, { importance: 3 }), moment(11, { importance: 9 }), moment(12, { importance: 3 }), moment(13)]
    // 13 ends on the next page, so it competes there and not with the three before it.
    expect(countedMoments(weighed, spans, 2).map((m) => m.id)).toEqual([10, 11, 13])
    expect(countedMoments(weighed, spans, 1).map((m) => m.id)).toEqual([11, 13])
    expect(countedMoments(weighed, spans, 3).map((m) => m.id)).toEqual([10, 11, 12, 13])
    expect(momentToShow(weighed, spans, 1, 12)?.id).toBe(11)
  })

  it('always counts a starred picture, on top of the others of its page', () => {
    const moments = [moment(14, { starred: true }), moment(15), moment(16, { starred: true })]
    expect(countedMoments(moments, spans, 1).map((m) => m.id)).toEqual([14, 15, 16])
    expect(momentToShow(moments, spans, 1, 16)?.id).toBe(16)
    expect(countedMoments([moment(14), moment(15), moment(16, { starred: true })], spans, 1).map((m) => m.id)).toEqual([14, 16])
  })

  it('counts a paragraph that runs over a page break on the page where it ends', () => {
    const moments = [moment(11), moment(12), moment(13), moment(14), moment(15)]
    expect(countedMoments(moments, spans, 2).map((m) => m.id)).toEqual([11, 12, 13, 14])
  })

  it('counts a picture that is not drawn yet, and shows the one before it meanwhile', () => {
    const moments = [moment(14), moment(15, { image_state: 'drawing' }), moment(16)]
    expect(shown(moments, 16)).toBe(14)
    expect(momentReached(moments, spans, 2, 16)?.id).toBe(15)
    expect(shown([moment(14, { image_state: 'failed' })], 16)).toBeNull()
  })

  it('takes the picture down at a clear, and not before the reading reaches it', () => {
    const moments = [moment(11), moment(15, { kind: 'clear', scene: '', image_state: 'none' }), moment(17)]
    expect(shown(moments, 14)).toBe(11)
    expect(shown(moments, 15)).toBeNull()
    expect(shown(moments, 16)).toBeNull()
    expect(shown(moments, 17)).toBe(17)
  })

  it('does not let clears use up the two pictures of a page', () => {
    const clear = { kind: 'clear', scene: '', image_state: 'none' } as const
    const moments = [moment(14, clear), moment(15), moment(16)]
    expect(countedMoments(moments, spans, 2).map((m) => m.id)).toEqual([14, 15, 16])
    expect(shown(moments, 16)).toBe(16)
  })

  it('ignores moments of paragraphs that are not laid out', () => {
    expect(shown([moment(3)], 18)).toBeNull()
  })
})

describe('illustrating', () => {
  it('needs an image service and the switch', () => {
    expect(illustrating({ image_url: 'http://gpu:8000', illustrations: true })).toBe(true)
    expect(illustrating({ image_url: 'http://gpu:8000', illustrations: false })).toBe(false)
    expect(illustrating({ image_url: '', illustrations: true })).toBe(false)
    expect(illustrating(null)).toBe(false)
  })
})
