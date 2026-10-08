import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react'
import { api, type Block, type Character, type CharacterInput, type Moment, type Segment } from '../api'
import { EMPTY_CHARACTER, sortCast } from '../lib/cast'
import { hueStyle } from './BlockView'
import { CharacterFields } from './CharacterFields'

interface Props {
  bookId: number
  segment: Segment
  block: Block
  characters: Character[]
  at: { x: number; y: number }
  /** Illustrations are on and the paragraph has no starred picture yet. */
  canStar: boolean
  onClose: () => void
  onSegment: (segment: Segment) => void
  onCharacter: (character: Character) => void
  onMoment: (moment: Moment) => void
}

const MARGIN = 12

function excerpt(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > 90 ? `${flat.slice(0, 88).trimEnd()}…` : flat
}

export function CorrectionPopover({ bookId, segment, block, characters, at, canStar, onClose, onSegment, onCharacter, onMoment }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState(at)
  const [draft, setDraft] = useState<CharacterInput | null>(null)
  const [busy, setBusy] = useState(false)
  const [starring, setStarring] = useState(false)
  const [message, setMessage] = useState<{ text: string; problem: boolean } | null>(null)

  useLayoutEffect(() => {
    const box = ref.current?.getBoundingClientRect()
    if (!box) return
    setPosition({
      x: Math.max(MARGIN, Math.min(at.x, window.innerWidth - box.width - MARGIN)),
      y: Math.max(MARGIN, Math.min(at.y, window.innerHeight - box.height - MARGIN)),
    })
  }, [at, draft])

  useEffect(() => {
    const onPointer = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('pointerdown', onPointer, true)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('pointerdown', onPointer, true)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const attempt = async (work: () => Promise<string | null>) => {
    setBusy(true)
    setMessage(null)
    try {
      const done = await work()
      if (done === null) onClose()
      else setMessage({ text: done, problem: false })
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : String(error), problem: true })
    } finally {
      setBusy(false)
    }
  }

  const assign = (characterId: number) =>
    attempt(async () => {
      onSegment(await api.setSpeaker(segment.id, characterId))
      return null
    })

  const createAndAssign = (event: FormEvent) => {
    event.preventDefault()
    if (!draft) return
    void attempt(async () => {
      const character = await api.createCharacter(bookId, { ...draft, name: draft.name.trim() })
      onCharacter(character)
      onSegment(await api.setSpeaker(segment.id, character.id))
      return null
    })
  }

  const regenerate = () =>
    attempt(async () => {
      onSegment(await api.regenerateSegment(segment.id))
      return 'Reading this line again.'
    })

  const retag = () =>
    attempt(async () => {
      await api.retagBlock(block.id)
      return 'Asked the tagger to look at this paragraph again.'
    })

  const star = () => {
    setStarring(true)
    return attempt(async () => {
      onMoment(await api.starBlock(block.id))
      return null
    }).finally(() => setStarring(false))
  }

  return (
    <div
      className="popover"
      ref={ref}
      role="dialog"
      aria-label="Correct this line"
      style={{ left: position.x, top: position.y }}
    >
      <p className="popover-excerpt">{excerpt(segment.text)}</p>

      {draft ? (
        <form onSubmit={createAndAssign}>
          <CharacterFields value={draft} onChange={setDraft} autoFocus />
          <div className="row-actions">
            <button type="button" className="quiet-button" onClick={() => setDraft(null)} disabled={busy}>
              Back
            </button>
            <button type="submit" className="button" disabled={busy || draft.name.trim() === ''}>
              Add and assign
            </button>
          </div>
        </form>
      ) : (
        <>
          <h3 className="popover-heading">Who says this?</h3>
          <ul className="speaker-list">
            {sortCast(characters).map((character) => (
              <li key={character.id}>
                <button
                  className="speaker-option"
                  onClick={() => void assign(character.id)}
                  disabled={busy}
                  aria-pressed={character.id === segment.character_id}
                >
                  <span
                    className="swatch"
                    data-neutral={character.role === 'narrator' || undefined}
                    style={hueStyle(character.id)}
                  />
                  <span className="speaker-option-name">{character.name}</span>
                  {character.role === 'narrator' && <span className="speaker-option-role">narrator</span>}
                </button>
              </li>
            ))}
          </ul>
          <div className="popover-actions">
            <button className="menu-button" onClick={() => setDraft(EMPTY_CHARACTER)} disabled={busy}>
              Someone new…
            </button>
            <button className="menu-button" onClick={() => void regenerate()} disabled={busy}>
              Read this line again
            </button>
            <button className="menu-button" onClick={() => void retag()} disabled={busy}>
              Tag this paragraph again
            </button>
            {canStar && (
              <>
                <button className="menu-button" onClick={() => void star()} disabled={busy} aria-busy={starring}>
                  {starring ? 'Asking what this moment shows…' : 'Draw a picture of this moment'}
                </button>
                <p className="popover-note">Makes one Claude or Codex call when the paragraph has no moment yet.</p>
              </>
            )}
          </div>
        </>
      )}

      {message && (
        <p className="popover-message" data-problem={message.problem || undefined} role="status">
          {message.text}
        </p>
      )}
    </div>
  )
}
