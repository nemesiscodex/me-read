import type { Block } from '../api'

/** Seconds of silence after a line, by how the text breaks there. */
export interface Pauses {
  heading: number
  paragraph: number
  sentence: number
  clause: number
  comma: number
  none: number
  speakerChange: number
}

export const PAUSE: Pauses = {
  heading: 1.1,
  paragraph: 0.7,
  sentence: 0.4,
  clause: 0.28,
  comma: 0.18,
  none: 0.1,
  speakerChange: 0.3,
}

export const PAUSE_MAX = 2.5
const STORAGE_KEY = 'me-read.pauses'

/** Saved pause lengths over the defaults; anything missing or out of range falls back to its default. */
export function readPauses(stored: string | null): Pauses {
  let saved: Record<string, unknown> = {}
  try {
    const parsed: unknown = stored === null ? null : JSON.parse(stored)
    if (parsed !== null && typeof parsed === 'object') saved = parsed as Record<string, unknown>
  } catch {
    // Unreadable settings are the same as none.
  }
  const pauses = { ...PAUSE }
  for (const kind of Object.keys(PAUSE) as (keyof Pauses)[]) {
    const value = saved[kind]
    if (typeof value === 'number' && value >= 0 && value <= PAUSE_MAX) pauses[kind] = value
  }
  return pauses
}

export function loadPauses(): Pauses {
  try {
    return readPauses(localStorage.getItem(STORAGE_KEY))
  } catch {
    return { ...PAUSE }
  }
}

export function savePauses(pauses: Pauses): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pauses))
  } catch {
    // Private mode: the setting lasts for this visit only.
  }
}

const CLOSERS = /[\s"'”’»)\]*_]+$/

function breakAfter(text: string, pause: Pauses): number {
  const last = text.replace(CLOSERS, '').slice(-1)
  if ('.!?…'.includes(last)) return pause.sentence
  if (';:'.includes(last)) return pause.clause
  if (',—–-'.includes(last)) return pause.comma
  return pause.none
}

/**
 * The silence to leave after each segment, keyed by segment id. Clips are trimmed tight, so
 * without this a paragraph runs straight into the next and a reply lands on top of the question.
 */
export function pausesAfter(blocks: Block[], pause: Pauses = PAUSE): Map<number, number> {
  const pauses = new Map<number, number>()
  for (const block of blocks) {
    block.segments.forEach((segment, at) => {
      const next = block.segments[at + 1]
      if (!next) {
        pauses.set(segment.id, block.tag === 'h' ? pause.heading : pause.paragraph)
        return
      }
      const changesSpeaker = next.character_id !== segment.character_id
      pauses.set(segment.id, Math.max(breakAfter(segment.text, pause), changesSpeaker ? pause.speakerChange : 0))
    })
  }
  return pauses
}
