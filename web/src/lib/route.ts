export type Route = { screen: 'library' } | { screen: 'reader'; bookId: number }

export function parseRoute(hash: string): Route {
  const match = /^#\/book\/(\d+)$/.exec(hash)
  return match ? { screen: 'reader', bookId: Number(match[1]) } : { screen: 'library' }
}

export const libraryHref = '#/'
export const bookHref = (bookId: number) => `#/book/${bookId}`
