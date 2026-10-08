import type { Block, BookDetail, Character, Segment, ServerEvent, TagState } from '../api'

export interface Activity {
  tagger: string | null
  speech: string | null
}

/** Everything the reader shows for one book: the cast and the single loaded chapter. */
export interface ReaderData {
  book: BookDetail
  /** `Chapter.index` of the loaded chapter. */
  chapterIndex: number
  /** Sorted by `idx`, each block's segments sorted by `seq`. */
  blocks: Block[]
  /** Bumped every time a chapter's blocks are replaced wholesale. */
  loadSeq: number
  activity: Activity
}

export type ReaderAction =
  | { type: 'opened'; book: BookDetail; chapterIndex: number; blocks: Block[] }
  | { type: 'bookRefreshed'; book: BookDetail }
  | { type: 'chapterLoaded'; chapterIndex: number; blocks: Block[] }
  | { type: 'event'; event: ServerEvent }
  | { type: 'segmentSaved'; segment: Segment }
  | { type: 'characterSaved'; character: Character }
  | { type: 'characterMerged'; id: number; into: number }

const bySeq = (a: Segment, b: Segment) => a.seq - b.seq

export function sortBlocks(blocks: Block[]): Block[] {
  return blocks
    .map((block) => ({ ...block, segments: [...block.segments].sort(bySeq) }))
    .sort((a, b) => a.idx - b.idx)
}

/** Replaces known segments by id and inserts unknown ones into their block. Segments of unloaded blocks are dropped. */
export function mergeSegments(blocks: Block[], incoming: Segment[]): Block[] {
  const byBlock = new Map<number, Segment[]>()
  for (const segment of incoming) {
    const list = byBlock.get(segment.block_id)
    if (list) list.push(segment)
    else byBlock.set(segment.block_id, [segment])
  }
  let changed = false
  const next = blocks.map((block) => {
    const updates = byBlock.get(block.id)
    if (!updates) return block
    changed = true
    const merged = new Map(block.segments.map((segment) => [segment.id, segment]))
    for (const segment of updates) merged.set(segment.id, segment)
    return { ...block, segments: [...merged.values()].sort(bySeq) }
  })
  return changed ? next : blocks
}

export function mergeTagStates(blocks: Block[], incoming: { id: number; tag_state: TagState }[]): Block[] {
  const states = new Map(incoming.map((b) => [b.id, b.tag_state]))
  let changed = false
  const next = blocks.map((block) => {
    const tagState = states.get(block.id)
    if (tagState === undefined || tagState === block.tag_state) return block
    changed = true
    return { ...block, tag_state: tagState }
  })
  return changed ? next : blocks
}

export function upsertCharacter(characters: Character[], character: Character): Character[] {
  return characters.some((c) => c.id === character.id)
    ? characters.map((c) => (c.id === character.id ? character : c))
    : [...characters, character]
}

/** Local picture of a merge until the server's events arrive: `id` disappears and its lines belong to `into`. */
export function mergeCharacterInto(data: ReaderData, id: number, into: number): ReaderData {
  const removed = data.book.characters.find((c) => c.id === id)
  if (!removed || id === into) return data
  const characters = data.book.characters
    .filter((c) => c.id !== id)
    .map((c) => (c.id === into ? { ...c, line_count: c.line_count + removed.line_count } : c))
  const blocks = data.blocks.map((block) =>
    block.segments.some((s) => s.character_id === id)
      ? { ...block, segments: block.segments.map((s) => (s.character_id === id ? { ...s, character_id: into } : s)) }
      : block,
  )
  return { ...data, book: { ...data.book, characters }, blocks }
}

function applyEvent(data: ReaderData, event: ServerEvent): ReaderData {
  switch (event.type) {
    case 'segments':
      return { ...data, blocks: mergeSegments(data.blocks, event.segments) }
    case 'blocks':
      return { ...data, blocks: mergeTagStates(data.blocks, event.blocks) }
    case 'characters':
      return { ...data, book: { ...data.book, characters: event.characters } }
    case 'activity':
      return { ...data, activity: { tagger: event.tagger, speech: event.speech } }
    default:
      return data
  }
}

export function readerReducer(data: ReaderData | null, action: ReaderAction): ReaderData | null {
  if (action.type === 'opened') {
    return {
      book: action.book,
      chapterIndex: action.chapterIndex,
      blocks: sortBlocks(action.blocks),
      loadSeq: (data?.loadSeq ?? 0) + 1,
      activity: { tagger: null, speech: null },
    }
  }
  if (!data) return data
  switch (action.type) {
    case 'bookRefreshed':
      return { ...data, book: action.book }
    case 'chapterLoaded':
      return { ...data, chapterIndex: action.chapterIndex, blocks: sortBlocks(action.blocks), loadSeq: data.loadSeq + 1 }
    case 'event':
      return applyEvent(data, action.event)
    case 'segmentSaved':
      return { ...data, blocks: mergeSegments(data.blocks, [action.segment]) }
    case 'characterSaved':
      return { ...data, book: { ...data.book, characters: upsertCharacter(data.book.characters, action.character) } }
    case 'characterMerged':
      return mergeCharacterInto(data, action.id, action.into)
  }
}
