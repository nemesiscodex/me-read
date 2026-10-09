import type { Moment, Settings } from '../api'
import type { BlockSpan } from './pages'

/**
 * Which picture the reader sees. Three rules live here.
 *
 * No spoilers: a moment counts only once the reading position has reached its paragraph. While the player has a
 * line (playing, paused or waiting) the position is that line's paragraph. Otherwise it is the last paragraph
 * before the page in view, so a moment on the page in view is never shown before it is read aloud or the page is
 * turned.
 *
 * A limit of pictures a page, which the reader sets: of the pictures on one page only the `limit` most important
 * count, the earlier one first when two matter as much. A paragraph that runs over a page break belongs to the page
 * it ends on, which is how the server counts when that page is the current one. A picture the reader starred always
 * counts and takes none of those places.
 *
 * A picture stays until something replaces it: the last counted picture that is drawn stays up on later pages,
 * until the next one or until a moment of kind 'clear', which says the picture stopped matching the text and takes
 * it down. Clears follow the spoiler rule and do not count toward the limit.
 *
 * Only the loaded chapter is looked at, so a new chapter starts with no picture until its first moment.
 */

/** Whether moments are looked for, drawn and shown: there is an image service and the reader has not switched it off. */
export function illustrating(settings: Pick<Settings, 'image_url' | 'illustrations'> | null): boolean {
  return settings !== null && settings.image_url !== '' && settings.illustrations
}

/** `activeIdx` is the `Block.idx` of the player's line, or null when it has none. */
export function readingPosition(spans: BlockSpan[], page: number, activeIdx: number | null): number {
  if (activeIdx !== null) return activeIdx
  const onPage = spans.filter((span) => span.firstPage <= page && span.lastPage >= page)
  return onPage.length > 0 ? Math.min(...onPage.map((span) => span.idx)) - 1 : -1
}

/** The moments that count, in reading order: every clear, every starred picture, and per page the `limit` most important others. */
export function countedMoments(moments: Moment[], spans: BlockSpan[], limit: number): Moment[] {
  const pageOf = new Map(spans.map((span) => [span.idx, span.lastPage]))
  const taken = new Map<number, number>()
  const counted = new Set<Moment>()
  for (const moment of [...moments].sort((a, b) => b.importance - a.importance || a.block_idx - b.block_idx)) {
    const page = pageOf.get(moment.block_idx)
    if (page === undefined) continue
    if (moment.kind === 'clear' || moment.starred) {
      counted.add(moment)
      continue
    }
    const before = taken.get(page) ?? 0
    taken.set(page, before + 1)
    if (before < limit) counted.add(moment)
  }
  return [...counted].sort((a, b) => a.block_idx - b.block_idx)
}

/** The last counted moment the reading has reached, whether or not its picture is drawn. */
export function momentReached(moments: Moment[], spans: BlockSpan[], limit: number, position: number): Moment | null {
  return countedMoments(moments, spans, limit).findLast((moment) => moment.block_idx <= position) ?? null
}

/** The picture to show at `position`, or null for none. */
export function momentToShow(moments: Moment[], spans: BlockSpan[], limit: number, position: number): Moment | null {
  const standing = countedMoments(moments, spans, limit).findLast(
    (moment) => moment.block_idx <= position && (moment.kind === 'clear' || moment.image_state === 'ready'),
  )
  return standing?.kind === 'picture' ? standing : null
}
