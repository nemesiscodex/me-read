import { useEffect, useState, type CSSProperties } from 'react'
import { segmentIdAt } from './BlockView'

export interface Hint {
  speaker: string
  hue: number | null
  note: string | null
  problem: boolean
}

interface Props {
  /** Null when a segment has nothing worth saying, which keeps plain narration quiet. */
  describe: (segmentId: number) => Hint | null
}

const DELAY_MS = 350

/** A small label above the hovered line: who says it, and why it cannot play yet. */
export function SegmentHint({ describe }: Props) {
  const [shown, setShown] = useState<{ id: number; x: number; y: number } | null>(null)

  useEffect(() => {
    let timer: number | undefined

    const hide = () => {
      window.clearTimeout(timer)
      setShown(null)
    }
    const over = (event: MouseEvent) => {
      const id = segmentIdAt(event.target)
      window.clearTimeout(timer)
      if (id === null) return setShown(null)
      const el = (event.target as Element).closest('[data-seg]')!
      const line = [...el.getClientRects()].find((r) => event.clientY >= r.top && event.clientY <= r.bottom)
      const next = { id, x: event.clientX, y: (line ?? el.getBoundingClientRect()).top }
      timer = window.setTimeout(() => setShown(next), DELAY_MS)
    }

    document.addEventListener('mouseover', over)
    document.addEventListener('mousedown', hide)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('mouseover', over)
      document.removeEventListener('mousedown', hide)
    }
  }, [])

  const hint = shown ? describe(shown.id) : null
  if (!shown || !hint) return null
  return (
    <div className="hint" role="tooltip" style={{ left: shown.x, top: shown.y }} data-problem={hint.problem || undefined}>
      {hint.hue !== null && <span className="swatch" style={{ '--h': hint.hue } as CSSProperties} />}
      <span className="hint-speaker">{hint.speaker}</span>
      {hint.note && <span className="hint-note">{hint.note}</span>}
    </div>
  )
}
