import { describe, expect, it } from 'vitest'
import type { BookDetail } from '../api'
import { block, character, moment, segment } from './fixtures'
import { mergeMoments, mergeSegments, mergeTagStates, readerReducer, upsertCharacter, type ReaderData } from './readerState'

const book: BookDetail = {
  id: 1,
  title: 'Book',
  author: 'Author',
  block_count: 2,
  position_block: 0,
  position_seq: 0,
  language: 'en',
  chapters: [{ index: 0, title: 'One', first_block: 0, block_count: 2 }],
  characters: [character(1, { role: 'narrator', line_count: 10 }), character(2, { line_count: 3 }), character(3, { line_count: 2 })],
}

function opened(): ReaderData {
  const blocks = [block(2, [segment(21, { character_id: 2 })]), block(1, [segment(12), segment(11)])]
  const moments = [moment(2), moment(1, { image_state: 'none' }), moment(77)]
  return readerReducer(null, { type: 'opened', book, chapterIndex: 0, blocks, moments })!
}

describe('opening a chapter', () => {
  it('puts blocks and their segments in reading order', () => {
    const data = opened()
    expect(data.blocks.map((b) => b.id)).toEqual([1, 2])
    expect(data.blocks[0]!.segments.map((s) => s.id)).toEqual([11, 12])
  })

  it('keeps the moments of its paragraphs, in reading order', () => {
    expect(opened().moments.map((m) => m.id)).toEqual([1, 2])
  })
})

describe('mergeMoments', () => {
  it('lets the starred picture of a paragraph take the place of the clear it had', () => {
    const blocks = [block(1, [segment(10)]), block(2, [segment(20)])]
    const before = [moment(1), moment(2, { kind: 'clear', scene: '', image_state: 'none' })]
    const starred = moment(9, { block_id: 2, block_idx: 2, starred: true })
    expect(mergeMoments(before, blocks, [starred])).toEqual([moment(1), starred])
  })

  it('replaces a moment by id, adds a new one at its place and drops those of other chapters', () => {
    const { blocks } = opened()
    const before = [moment(2)]
    const merged = mergeMoments(before, blocks, [moment(2, { image_rev: 3 }), moment(5, { block_id: 1, block_idx: 1 }), moment(77)])
    expect(merged.map((m) => [m.id, m.image_rev])).toEqual([[5, 0], [2, 3]])
    expect(mergeMoments(before, blocks, [moment(77)])).toBe(before)
  })
})

describe('mergeSegments', () => {
  it('replaces a segment by id and leaves untouched blocks as they were', () => {
    const { blocks } = opened()
    const merged = mergeSegments(blocks, [{ ...blocks[0]!.segments[1]!, audio_state: 'generating' }])
    expect(merged[0]!.segments.map((s) => s.audio_state)).toEqual(['ready', 'generating'])
    expect(merged[1]).toBe(blocks[1])
  })

  it('inserts a segment it has not seen at its place in the block', () => {
    const { blocks } = opened()
    const merged = mergeSegments(blocks, [segment(99, { block_id: 1, seq: 11.5 })])
    expect(merged[0]!.segments.map((s) => s.id)).toEqual([11, 99, 12])
  })

  it('ignores segments of blocks that are not loaded', () => {
    const { blocks } = opened()
    expect(mergeSegments(blocks, [segment(500, { block_id: 77 })])).toBe(blocks)
  })
})

describe('mergeTagStates', () => {
  it('updates only the named blocks', () => {
    const { blocks } = opened()
    const merged = mergeTagStates(blocks, [{ id: 2, tag_state: 'tagging' }, { id: 77, tag_state: 'failed' }])
    expect(merged.map((b) => b.tag_state)).toEqual(['tagged', 'tagging'])
    expect(merged[0]).toBe(blocks[0])
  })

  it('keeps the same list when nothing changed', () => {
    const { blocks } = opened()
    expect(mergeTagStates(blocks, [{ id: 1, tag_state: 'tagged' }])).toBe(blocks)
  })
})

describe('server events', () => {
  it('replaces the cast', () => {
    const data = readerReducer(opened(), { type: 'event', event: { type: 'characters', characters: [character(9)] } })!
    expect(data.book.characters.map((c) => c.id)).toEqual([9])
  })

  it('records what the workers are doing', () => {
    const activity = { tagger: 'Tagging 3', scenes: null, speech: null, images: 'Drawing the scene' }
    const data = readerReducer(opened(), { type: 'event', event: { type: 'activity', ...activity } })!
    expect(data.activity).toEqual(activity)
  })

  it('shows a picture that was just drawn and a redraw that was just asked for', () => {
    const drawn = readerReducer(opened(), { type: 'event', event: { type: 'moments', moments: [moment(1), moment(77)] } })!
    expect(drawn.moments.map((m) => [m.id, m.image_state])).toEqual([[1, 'ready'], [2, 'ready']])
    const asked = readerReducer(drawn, { type: 'momentSaved', moment: moment(2, { image_state: 'none', image_rev: 1 }) })!
    expect(asked.moments[1]).toMatchObject({ image_state: 'none', image_rev: 1 })
  })

  it('ignores an event type it does not know', () => {
    const before = opened()
    expect(readerReducer(before, { type: 'event', event: { type: 'future' } as never })).toBe(before)
  })

  it('drops events that arrive before the book is open', () => {
    expect(readerReducer(null, { type: 'event', event: { type: 'segments', segments: [segment(1)] } })).toBeNull()
  })
})

describe('cast edits', () => {
  it('adds a new character and replaces an edited one in place', () => {
    const added = upsertCharacter(book.characters, character(4))
    expect(added.map((c) => c.id)).toEqual([1, 2, 3, 4])
    const renamed = upsertCharacter(added, character(2, { name: 'Renamed' }))
    expect(renamed.map((c) => c.name)[1]).toBe('Renamed')
  })

  it('moves a merged character’s lines and count to the survivor', () => {
    const data = readerReducer(opened(), { type: 'characterMerged', id: 2, into: 3 })!
    expect(data.book.characters.map((c) => [c.id, c.line_count])).toEqual([[1, 10], [3, 5]])
    expect(data.blocks[1]!.segments[0]!.character_id).toBe(3)
  })
})

describe('loading another chapter', () => {
  it('replaces the blocks and marks a fresh layout', () => {
    const before = opened()
    const after = readerReducer(before, { type: 'chapterLoaded', chapterIndex: 1, blocks: [block(7, [segment(70)])], moments: [moment(7)] })!
    expect(after.chapterIndex).toBe(1)
    expect(after.blocks.map((b) => b.id)).toEqual([7])
    expect(after.moments.map((m) => m.id)).toEqual([7])
    expect(after.loadSeq).toBeGreaterThan(before.loadSeq)
  })
})
