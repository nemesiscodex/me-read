import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, type Character, type CharacterInput, type VoiceState } from '../api'
import { Icon } from '../Icon'
import { changedFields, inputOf, sortCast } from '../lib/cast'
import { hueStyle } from './BlockView'
import { CharacterFields } from './CharacterFields'

interface Props {
  characters: Character[]
  onClose: () => void
  onCharacter: (character: Character) => void
  onMerged: (id: number, into: number) => void
  /** Called before a voice sample starts so the book can stop talking over it. */
  onSample: () => void
}

const VOICE_NOTE: Record<VoiceState, string> = {
  none: 'no voice yet',
  designing: 'designing a voice…',
  ready: 'voice ready',
  failed: 'voice failed',
}

export function CastPanel({ characters, onClose, onCharacter, onMerged, onSample }: Props) {
  const [openId, setOpenId] = useState<number | null>(null)
  const [sampleOf, setSampleOf] = useState<string | null>(null)
  const sample = useRef<HTMLAudioElement | null>(null)

  const stopSample = () => {
    sample.current?.pause()
    sample.current = null
    setSampleOf(null)
  }

  useEffect(() => () => sample.current?.pause(), [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const toggleSample = (key: string, url: string) => {
    const wasPlaying = sampleOf === key
    stopSample()
    if (wasPlaying) return
    onSample()
    const el = new Audio(url)
    el.onended = stopSample
    el.onerror = stopSample
    sample.current = el
    setSampleOf(key)
    el.play().catch(stopSample)
  }

  return (
    <aside className="drawer" aria-label="Cast">
      <header className="drawer-header">
        <h2>Cast</h2>
        <button className="icon-button" onClick={onClose} aria-label="Close cast">
          <Icon name="close" />
        </button>
      </header>
      <ul className="cast-list">
        {sortCast(characters).map((character) => (
          <li key={character.id} className="cast-item" data-open={openId === character.id || undefined}>
            <div className="cast-row">
              <span
                className="swatch swatch-large"
                data-neutral={character.role === 'narrator' || undefined}
                style={hueStyle(character.id)}
              />
              <button
                className="cast-summary"
                onClick={() => setOpenId(openId === character.id ? null : character.id)}
                aria-expanded={openId === character.id}
              >
                <span className="cast-name">{character.name}</span>
                <span className="cast-meta">
                  {character.role}, {character.gender}, {character.age}, {character.pitch} pitch
                </span>
                <span className="cast-meta">
                  {character.line_count} {character.line_count === 1 ? 'line' : 'lines'},{' '}
                  <span className="voice-note" data-voice={character.voice_state}>
                    {VOICE_NOTE[character.voice_state]}
                  </span>
                </span>
              </button>
              <button
                className="icon-button"
                onClick={() => toggleSample(`voice-${character.id}`, api.voiceUrl(character))}
                disabled={character.voice_state !== 'ready'}
                aria-label={
                  sampleOf === `voice-${character.id}` ? `Stop ${character.name}'s sample` : `Hear ${character.name}`
                }
              >
                <Icon name={sampleOf === `voice-${character.id}` ? 'pause' : 'play'} />
              </button>
            </div>
            {character.voice_state === 'failed' && character.error && (
              <p className="cast-error">{character.error}</p>
            )}
            {openId === character.id && (
              <CharacterEditor
                key={character.id}
                character={character}
                others={characters.filter((c) => c.id !== character.id)}
                onCharacter={onCharacter}
                candidatePlaying={sampleOf === `draft-${character.id}`}
                onCandidate={() => toggleSample(`draft-${character.id}`, api.draftUrl(character))}
                onMerged={(into) => {
                  setOpenId(null)
                  onMerged(character.id, into)
                }}
              />
            )}
          </li>
        ))}
      </ul>
    </aside>
  )
}

interface EditorProps {
  character: Character
  others: Character[]
  onCharacter: (character: Character) => void
  candidatePlaying: boolean
  onCandidate: () => void
  onMerged: (into: number) => void
}

function CharacterEditor({ character, others, onCharacter, candidatePlaying, onCandidate, onMerged }: EditorProps) {
  const [draft, setDraft] = useState<CharacterInput>(() => inputOf(character))
  const [mergeInto, setMergeInto] = useState<number | ''>('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const changes = changedFields(inputOf(character), { ...draft, name: draft.name.trim() })
  const dirty = Object.keys(changes).length > 0

  const attempt = async (work: () => Promise<void>) => {
    setBusy(true)
    setProblem(null)
    try {
      await work()
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const save = (event: FormEvent) => {
    event.preventDefault()
    void attempt(async () => {
      const saved = await api.updateCharacter(character.id, changes)
      onCharacter(saved)
      setDraft(inputOf(saved))
    })
  }

  const act = (call: (id: number) => Promise<Character>) =>
    void attempt(async () => onCharacter(await call(character.id)))
  const lines = character.line_count
  const target = others.find((c) => c.id === mergeInto)

  return (
    <div className="cast-editor">
      <form onSubmit={save}>
        <CharacterFields value={draft} onChange={setDraft} />
        <div className="row-actions">
          <button type="submit" className="button" disabled={busy || !dirty || draft.name.trim() === ''}>
            Save changes
          </button>
        </div>
        <p className="candidate-note">The description shapes the next voice you try. The current voice stays until you replace it.</p>
      </form>

      {character.draft_state !== 'none' && (
        <div className="candidate" data-draft={character.draft_state}>
          {character.draft_state === 'ready' ? (
            <>
              <p className="candidate-note">
                A candidate voice is ready. Nothing changes until you use it; then {character.name}'s{' '}
                {lines === 1 ? 'line is' : `${lines} lines are`} read again.
              </p>
              <div className="candidate-actions">
                <button className="quiet-button" onClick={onCandidate}>
                  <Icon name={candidatePlaying ? 'pause' : 'play'} /> {candidatePlaying ? 'Stop' : 'Listen'}
                </button>
                <button className="button" disabled={busy} onClick={() => act(api.approveDraft)}>
                  Use this voice
                </button>
                <button className="quiet-button" disabled={busy} onClick={() => act(api.requestDraft)}>
                  Try another
                </button>
                <button className="quiet-button" disabled={busy} onClick={() => act(api.discardDraft)}>
                  Discard
                </button>
              </div>
            </>
          ) : character.draft_state === 'failed' ? (
            <p className="cast-error">The candidate voice could not be designed.</p>
          ) : (
            <div className="candidate-actions">
              <p className="candidate-note candidate-busy">Designing a candidate voice…</p>
              <button className="quiet-button" disabled={busy} onClick={() => act(api.discardDraft)}>
                Cancel
              </button>
            </div>
          )}
        </div>
      )}

      <div className="cast-voice-actions">
        {(character.draft_state === 'none' || character.draft_state === 'failed') && (
          <button className="quiet-button" disabled={busy} onClick={() => act(api.requestDraft)}>
            Try a new voice
          </button>
        )}
        <button className="quiet-button" disabled={busy} onClick={() => fileInput.current?.click()}>
          Use my own recording…
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="audio/*"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file) void attempt(async () => onCharacter(await api.uploadVoice(character.id, file)))
          }}
        />
      </div>

      {character.role !== 'narrator' && others.length > 0 && (
        <div className="cast-merge">
          <label className="field">
            <span>Same person as</span>
            <select value={mergeInto} onChange={(event) => setMergeInto(event.target.value ? Number(event.target.value) : '')}>
              <option value="">Choose a character</option>
              {sortCast(others).map((other) => (
                <option key={other.id} value={other.id}>
                  {other.name}
                </option>
              ))}
            </select>
          </label>
          {target && (
            <button
              className="button danger"
              disabled={busy}
              onClick={() =>
                void attempt(async () => {
                  await api.mergeCharacter(character.id, target.id)
                  onMerged(target.id)
                })
              }
            >
              Merge {character.name} into {target.name}
            </button>
          )}
        </div>
      )}

      {problem && <p className="cast-error">{problem}</p>}
    </div>
  )
}
