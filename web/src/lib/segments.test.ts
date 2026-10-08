import { describe, expect, it } from 'vitest'
import { block, segment } from './fixtures'
import { nextPlayable, playability, playItems, segmentStatus } from './segments'

const untagged = { kind: 'speech', character_id: null, audio_state: 'none' } as const

describe('segmentStatus', () => {
  it('follows the block tag state while a spoken line has no speaker', () => {
    expect(segmentStatus(segment(1, untagged), 'pending')).toBe('untagged')
    expect(segmentStatus(segment(1, untagged), 'tagging')).toBe('tagging')
    expect(segmentStatus(segment(1, untagged), 'failed')).toBe('tag-failed')
  })

  it('reports narration in an untagged block as waiting for audio, not for the tagger', () => {
    expect(segmentStatus(segment(1, { audio_state: 'none' }), 'tagging')).toBe('waiting')
  })

  it('reports the audio state once a line has a speaker', () => {
    const tagged = { kind: 'speech', character_id: 4 } as const
    expect(segmentStatus(segment(1, { ...tagged, audio_state: 'none' }), 'tagged')).toBe('waiting')
    expect(segmentStatus(segment(1, { ...tagged, audio_state: 'generating' }), 'tagged')).toBe('generating')
    expect(segmentStatus(segment(1, { ...tagged, audio_state: 'ready' }), 'tagged')).toBe('ready')
    expect(segmentStatus(segment(1, { ...tagged, audio_state: 'failed' }), 'tagged')).toBe('failed')
  })
})

describe('playability', () => {
  it('skips lines that will never have audio', () => {
    expect(playability(segment(1, { audio_state: 'failed' }), 'tagged')).toBe('skip')
    expect(playability(segment(1, untagged), 'failed')).toBe('skip')
    expect(playability(segment(1, { audio_state: 'none', text: ' — \n' }), 'tagged')).toBe('skip')
  })

  it('waits for lines that are still being prepared', () => {
    expect(playability(segment(1, untagged), 'pending')).toBe('pending')
    expect(playability(segment(1, { audio_state: 'generating' }), 'tagged')).toBe('pending')
  })

  it('plays a ready clip even when its text has nothing to say', () => {
    expect(playability(segment(1, { text: '…' }), 'tagged')).toBe('ready')
  })
})

describe('reading order', () => {
  const blocks = [
    block(1, [segment(10), segment(11, { audio_state: 'failed' })]),
    block(2, [segment(20, { audio_state: 'generating' }), segment(21)]),
  ]

  it('lists segments block by block', () => {
    expect(playItems(blocks).map((item) => item.id)).toEqual([10, 11, 20, 21])
  })

  it('finds the next line that can play, across blocks and past failed ones', () => {
    const items = playItems(blocks)
    expect(nextPlayable(items, 10)).toEqual({ id: 20, playability: 'pending' })
    expect(nextPlayable(items, 21)).toBeNull()
    expect(nextPlayable(items, 999)).toBeNull()
  })
})
