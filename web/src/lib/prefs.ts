import { useCallback, useState } from 'react'

export interface NumberPref {
  key: string
  fallback: number
  min: number
  max: number
}

export const FONT_SIZE: NumberPref = { key: 'me-read.font-px', fallback: 19, min: 14, max: 30 }
export const SPEED: NumberPref = { key: 'me-read.speed', fallback: 1, min: 0.8, max: 1.5 }

export function clampPref(pref: NumberPref, value: number): number {
  return Number.isFinite(value) ? Math.min(pref.max, Math.max(pref.min, value)) : pref.fallback
}

function read(pref: NumberPref): number {
  try {
    const stored = localStorage.getItem(pref.key)
    return stored === null ? pref.fallback : clampPref(pref, Number(stored))
  } catch {
    return pref.fallback
  }
}

export function useNumberPref(pref: NumberPref): [number, (value: number) => void] {
  const [value, setValue] = useState(() => read(pref))
  const set = useCallback(
    (next: number) => {
      const clamped = clampPref(pref, next)
      setValue(clamped)
      try {
        localStorage.setItem(pref.key, String(clamped))
      } catch {
        // Private mode: the setting lasts for this visit only.
      }
    },
    [pref],
  )
  return [value, set]
}
