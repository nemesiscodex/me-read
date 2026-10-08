import { useEffect, useRef, useState } from 'react'
import { PAUSE, PAUSE_MAX, type Pauses } from '../lib/pauses'

const KINDS: { kind: keyof Pauses; label: string }[] = [
  { kind: 'heading', label: 'After a heading' },
  { kind: 'paragraph', label: 'Between paragraphs' },
  { kind: 'sentence', label: 'After a sentence' },
  { kind: 'clause', label: 'After ; or :' },
  { kind: 'comma', label: 'After a comma or dash' },
  { kind: 'speakerChange', label: 'When the voice changes' },
  { kind: 'none', label: 'Anywhere else' },
]

/** Lets the reader set how long playback rests after each kind of break. Changes apply from the next line. */
export function PauseSettings({ value, onChange }: { value: Pauses; onChange: (pauses: Pauses) => void }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('pointerdown', onPointer)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('pointerdown', onPointer)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const changed = KINDS.some(({ kind }) => value[kind] !== PAUSE[kind])

  return (
    <div className="pause-settings" ref={root}>
      <button className="quiet-button" onClick={() => setOpen(!open)} aria-expanded={open}>
        Pauses
      </button>
      {open && (
        <div className="pause-panel" role="dialog" aria-label="Pauses between lines">
          <p className="popover-heading">Silence after each line, in seconds</p>
          {KINDS.map(({ kind, label }) => (
            <label key={kind} className="pause-row">
              <span>{label}</span>
              <input
                type="range"
                min={0}
                max={PAUSE_MAX}
                step={0.05}
                value={value[kind]}
                onChange={(event) => onChange({ ...value, [kind]: Number(event.target.value) })}
              />
              <span className="pause-value">{value[kind].toFixed(2)}</span>
            </label>
          ))}
          <div className="row-actions">
            <button className="quiet-button" disabled={!changed} onClick={() => onChange({ ...PAUSE })}>
              Reset to defaults
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
