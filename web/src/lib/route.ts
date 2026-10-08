import type { Place } from './pages'

export type Route = { screen: 'library' } | { screen: 'reader'; bookId: number; place: Place | null }

/** `#/book/2/p/153.4` is book 2 at paragraph 153, fourth line in. Without the place the book opens where it was left. */
export function parseRoute(hash: string): Route {
  const match = /^#\/book\/(\d+)(?:\/p\/(\d+)(?:\.(\d+))?)?$/.exec(hash)
  if (!match) return { screen: 'library' }
  const place = match[2] === undefined ? null : { block: Number(match[2]), seq: Number(match[3] ?? 0) }
  return { screen: 'reader', bookId: Number(match[1]), place }
}

export const libraryHref = '#/'

export function bookHref(bookId: number, place?: Place | null): string {
  if (!place) return `#/book/${bookId}`
  return `#/book/${bookId}/p/${place.block}${place.seq > 0 ? `.${place.seq}` : ''}`
}
