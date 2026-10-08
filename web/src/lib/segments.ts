import type { Block, Segment, TagState } from '../api'

/** What one segment looks like on the page. Exactly one applies at a time. */
export type SegmentStatus =
  | 'untagged'
  | 'tagging'
  | 'tag-failed'
  | 'waiting'
  | 'generating'
  | 'ready'
  | 'failed'

/** Whether playback can use a segment now, later, or never. */
export type Playability = 'ready' | 'pending' | 'skip'

export interface PlayItem {
  id: number
  playability: Playability
}

export interface LocatedSegment {
  segment: Segment
  block: Block
}

const AUDIO_STATUS = {
  ready: 'ready',
  failed: 'failed',
  generating: 'generating',
} as const

const UNTAGGED_STATUS: Record<TagState, SegmentStatus> = {
  pending: 'untagged',
  tagging: 'tagging',
  failed: 'tag-failed',
  tagged: 'untagged',
}

export function isUntagged(segment: Segment): boolean {
  return segment.kind === 'speech' && segment.character_id === null
}

export function segmentStatus(segment: Segment, tagState: TagState): SegmentStatus {
  if (segment.audio_state !== 'none') return AUDIO_STATUS[segment.audio_state]
  return isUntagged(segment) ? UNTAGGED_STATUS[tagState] : 'waiting'
}

const SPEAKABLE = /[\p{L}\p{N}]/u

export function playability(segment: Segment, tagState: TagState): Playability {
  if (segment.audio_state === 'ready') return 'ready'
  if (segment.audio_state === 'failed') return 'skip'
  if (!SPEAKABLE.test(segment.text)) return 'skip'
  if (isUntagged(segment) && tagState === 'failed') return 'skip'
  return 'pending'
}

/** Every segment of the chapter in reading order. Blocks and segments must already be sorted. */
export function playItems(blocks: Block[]): PlayItem[] {
  return blocks.flatMap((block) =>
    block.segments.map((segment) => ({ id: segment.id, playability: playability(segment, block.tag_state) })),
  )
}

export function indexSegments(blocks: Block[]): Map<number, LocatedSegment> {
  const index = new Map<number, LocatedSegment>()
  for (const block of blocks) for (const segment of block.segments) index.set(segment.id, { segment, block })
  return index
}

/** The next item playback would move to after `id`, skipping the ones that can never play. */
export function nextPlayable(items: PlayItem[], id: number): PlayItem | null {
  const at = items.findIndex((item) => item.id === id)
  if (at === -1) return null
  return items.slice(at + 1).find((item) => item.playability !== 'skip') ?? null
}
