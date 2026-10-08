import { describe, expect, it } from 'vitest'
import type { BookDetail } from '../api'
import { block, character, segment } from './fixtures'
import { mergeSegments, mergeTagStates, readerReducer, upsertCharacter, type ReaderData } from './readerState'

const book: BookDetail = {
  id: 1,
  title: 'Book',
  author: 'Author',
  block_count: 2,
  position_block: 0,
  language: 'en',
  chapters: [{ index: 0, title: 'One', first_block: 0, block_count: 2 }],
  characters: [character(1, { role: 'narrator', line_count: 10 }), character(2, { line_count: 3 }), character(3, { line_count: 2 })],
}

function opened(): ReaderData {
  const blocks = [block(2, [segment(21, { character_id: 2 })]), block(1, [segment(12), segment(11)])]
  return readerReducer(null, { type: 'opened', book, chapterIndex: 0, blocks })!
}

describe('opening a chapter', () => {
  it('puts blocks and their segments in reading order', () => {
    const data = opened()
    expect(data.blocks.map((b) => b.id)).toEqual([1, 2])
    expect(data.blocks[0]!.segments.map((s) => s.id)).toEqual([11, 12])
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
    const data = readerReducer(opened(), { type: 'event', event: { type: 'activity', tagger: 'Tagging 3', speech: null } })!
    expect(data.activity).toEqual({ tagger: 'Tagging 3', speech: null })
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
    const after = readerReducer(before, { type: 'chapterLoaded', chapterIndex: 1, blocks: [block(7, [segment(70)])] })!
    expect(after.chapterIndex).toBe(1)
    expect(after.blocks.map((b) => b.id)).toEqual([7])
    expect(after.loadSeq).toBeGreaterThan(before.loadSeq)
  })
})
