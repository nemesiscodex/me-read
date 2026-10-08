import { describe, expect, it } from 'vitest'
import { changedFields, characterHue, inputOf, narratorOf, sortCast } from './cast'
import { character } from './fixtures'
import { bookHref, parseRoute } from './route'

describe('characterHue', () => {
  it('gives the same id the same hue every time, inside the colour wheel', () => {
    for (const id of [1, 2, 17, 4096]) {
      expect(characterHue(id)).toBe(characterHue(id))
      expect(characterHue(id)).toBeGreaterThanOrEqual(0)
      expect(characterHue(id)).toBeLessThan(360)
    }
  })

  it('keeps the first twelve characters at least 20 degrees apart', () => {
    const hues = Array.from({ length: 12 }, (_, i) => characterHue(i + 1))
    for (const [i, a] of hues.entries()) {
      for (const b of hues.slice(i + 1)) {
        const distance = Math.min(Math.abs(a - b), 360 - Math.abs(a - b))
        expect(distance).toBeGreaterThanOrEqual(20)
      }
    }
  })
})

describe('cast', () => {
  const cast = [
    character(1, { role: 'extra', line_count: 40 }),
    character(2, { line_count: 5 }),
    character(3, { role: 'narrator', line_count: 1 }),
    character(4, { line_count: 9 }),
  ]

  it('lists the narrator, then named characters, then extras, busiest first', () => {
    expect(sortCast(cast).map((c) => c.id)).toEqual([3, 4, 2, 1])
  })

  it('finds the narrator', () => {
    expect(narratorOf(cast)?.id).toBe(3)
    expect(narratorOf([character(1)])).toBeNull()
  })

  it('sends only the fields that were edited', () => {
    const before = inputOf(character(1, { name: 'Ann', pitch: 'low' }))
    expect(changedFields(before, { ...before, name: 'Anne' })).toEqual({ name: 'Anne' })
    expect(changedFields(before, before)).toEqual({})
  })
})

describe('parseRoute', () => {
  it('opens a book by the id in the hash', () => {
    expect(parseRoute('#/book/12')).toEqual({ screen: 'reader', bookId: 12, place: null })
  })

  it('reads the line to open at, and writes it back the same way', () => {
    expect(parseRoute('#/book/12/p/153.4')).toEqual({ screen: 'reader', bookId: 12, place: { block: 153, seq: 4 } })
    expect(parseRoute('#/book/12/p/153')).toEqual({ screen: 'reader', bookId: 12, place: { block: 153, seq: 0 } })
    expect(bookHref(12, { block: 153, seq: 4 })).toBe('#/book/12/p/153.4')
    expect(bookHref(12, { block: 153, seq: 0 })).toBe('#/book/12/p/153')
    expect(bookHref(12)).toBe('#/book/12')
  })

  it('shows the library for anything else', () => {
    for (const hash of ['', '#/', '#/book/', '#/book/abc', '#/book/3/extra', '#/book/3/p/']) {
      expect(parseRoute(hash)).toEqual({ screen: 'library' })
    }
  })
})
