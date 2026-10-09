import type { Block, Character, Moment, Segment } from '../api'

export function segment(id: number, patch: Partial<Segment> = {}): Segment {
  return {
    id,
    block_id: 1,
    seq: id,
    text: `Line ${id}. `,
    kind: 'narration',
    character_id: 1,
    audio_state: 'ready',
    audio_rev: 1,
    duration: 1,
    error: null,
    ...patch,
  }
}

export function block(id: number, segments: Segment[], patch: Partial<Block> = {}): Block {
  return {
    id,
    idx: id,
    chapter: 0,
    tag: 'p',
    tag_state: 'tagged',
    segments: segments.map((s) => ({ ...s, block_id: id })),
    ...patch,
  }
}

export function character(id: number, patch: Partial<Character> = {}): Character {
  return {
    id,
    book_id: 1,
    name: `Character ${id}`,
    role: 'named',
    gender: 'unknown',
    age: 'adult',
    pitch: 'medium',
    description: '',
    voice_state: 'ready',
    voice_rev: 1,
    draft_state: 'none',
    draft_rev: 0,
    appearance: '',
    portrait_state: 'none',
    portrait_rev: 0,
    portrait_error: null,
    error: null,
    line_count: 0,
    ...patch,
  }
}

/** A drawn picture on the block with the same number. */
export function moment(id: number, patch: Partial<Moment> = {}): Moment {
  return {
    id,
    block_id: id,
    block_idx: id,
    kind: 'picture',
    title: `Plate ${id}`,
    summary: `What happens in ${id}`,
    scene: `Scene ${id}`,
    importance: 5,
    character_ids: [],
    image_state: 'ready',
    image_rev: 0,
    error: null,
    starred: false,
    ...patch,
  }
}
