import { describe, expect, it } from 'vitest'
import type { Block, Segment } from '../api'
import { PAUSE, pausesAfter, readPauses } from './pauses'

let nextId = 1
const seg = (text: string, character_id: number | null = 1): Segment => ({
  id: nextId++,
  block_id: 1,
  seq: 0,
  text,
  kind: 'narration',
  character_id,
  audio_state: 'ready',
  audio_rev: 0,
  duration: 1,
  error: null,
})
const block = (tag: 'p' | 'h', segments: Segment[]): Block => ({ id: nextId++, idx: 0, chapter: 0, tag, tag_state: 'tagged', segments })

describe('pausesAfter', () => {
  it('rests longest after a heading, then after a paragraph', () => {
    const title = seg('CHAPTER I.')
    const last = seg('She ran after it,')
    const pauses = pausesAfter([block('h', [title]), block('p', [last])])
    expect(pauses.get(title.id)).toBe(PAUSE.heading)
    expect(pauses.get(last.id)).toBe(PAUSE.paragraph)
  })

  it('follows the punctuation inside a paragraph when the same voice continues', () => {
    const sentence = seg('The gate creaked. ')
    const clause = seg('He waited; ')
    const comma = seg('and then, ')
    const running = seg('she saw a rabbit that ')
    const end = seg('ran.')
    const pauses = pausesAfter([block('p', [sentence, clause, comma, running, end])])
    expect([sentence, clause, comma, running].map((s) => pauses.get(s.id))).toEqual([
      PAUSE.sentence,
      PAUSE.clause,
      PAUSE.comma,
      PAUSE.none,
    ])
  })

  it('looks through closing quotes for the punctuation', () => {
    const question = seg('“Who goes there?” ', 2)
    const tag = seg('called the guard, ', 2)
    expect(pausesAfter([block('p', [question, tag, seg('twice.', 2)])]).get(question.id)).toBe(PAUSE.sentence)
  })

  it('leaves room when another voice takes over mid-paragraph', () => {
    const quote = seg('“Only me,”', 2)
    const tag = seg(' said Tom', 1)
    const pauses = pausesAfter([block('p', [quote, tag, seg('“Open up.”', 2)])])
    expect(pauses.get(quote.id)).toBe(PAUSE.speakerChange)
    expect(pauses.get(tag.id)).toBe(PAUSE.speakerChange)
  })

  it('uses the lengths the reader chose', () => {
    const sentence = seg('The gate creaked. ')
    const last = seg('Nobody spoke.')
    const pauses = pausesAfter([block('p', [sentence, last])], { ...PAUSE, sentence: 1.5, paragraph: 0 })
    expect([pauses.get(sentence.id), pauses.get(last.id)]).toEqual([1.5, 0])
  })
})

describe('readPauses', () => {
  it('keeps saved lengths and falls back to the default for anything unusable', () => {
    const stored = JSON.stringify({ sentence: 0.9, comma: -1, heading: 'long', paragraph: 99, extra: 1 })
    expect(readPauses(stored)).toEqual({ ...PAUSE, sentence: 0.9 })
    expect(readPauses('not json')).toEqual(PAUSE)
    expect(readPauses(null)).toEqual(PAUSE)
  })
})
