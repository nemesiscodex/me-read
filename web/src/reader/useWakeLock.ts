import { useEffect } from 'react'

/** Keep the visible reader awake for the whole run, including gaps and buffering. */
export function useWakeLock(running: boolean) {
  useEffect(() => {
    if (!running || !('wakeLock' in navigator)) return
    let disposed = false
    let pending = false
    let lock: WakeLockSentinel | null = null

    const acquire = async () => {
      if (disposed || pending || lock || document.visibilityState !== 'visible') return
      pending = true
      try {
        const acquired = await navigator.wakeLock.request('screen')
        if (disposed || document.visibilityState !== 'visible') {
          await acquired.release()
          return
        }
        if (!acquired.released) {
          lock = acquired
          acquired.addEventListener('release', () => {
            if (lock === acquired) lock = null
          }, { once: true })
        }
      } catch {
        // Browser policy or battery saver may deny a lock; audio must still work.
      } finally {
        pending = false
      }
    }

    // Browsers release screen locks when hidden. Take a new one on return.
    document.addEventListener('visibilitychange', acquire)
    void acquire()
    return () => {
      disposed = true
      document.removeEventListener('visibilitychange', acquire)
      if (lock) void lock.release().catch(() => {})
    }
  }, [running])
}
