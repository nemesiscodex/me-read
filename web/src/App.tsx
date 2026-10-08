import { useSyncExternalStore } from 'react'
import { Library } from './library/Library'
import { parseRoute } from './lib/route'
import { Reader } from './reader/Reader'

function subscribe(onChange: () => void) {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

export function App() {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash)
  const route = parseRoute(hash)
  return route.screen === 'reader' ? <Reader key={route.bookId} bookId={route.bookId} /> : <Library />
}
