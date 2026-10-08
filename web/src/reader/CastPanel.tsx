import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, type Character, type CharacterInput, type Settings, type VoiceState } from '../api'
import { Icon } from '../Icon'
import { changedFields, inputOf, sortCast } from '../lib/cast'
import { illustrating } from '../lib/moments'
import { hueStyle } from './BlockView'
import { CharacterFields } from './CharacterFields'

interface Props {
  characters: Character[]
  /** Portraits and looks are shown once there is an image service, also while pictures are switched off. */
  settings: Settings | null
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

function portraitNote(character: Character, drawing: boolean): string {
  if (character.appearance === '') return 'No portrait yet. It is drawn from the appearance.'
  switch (character.portrait_state) {
    case 'ready':
      return 'Scenes with this character are drawn from this portrait.'
    case 'drawing':
      return 'Drawing the portrait…'
    case 'failed':
      return `The portrait could not be drawn. ${character.portrait_error ?? ''}`
    case 'none':
      return drawing ? 'Waiting to be drawn.' : 'Pictures are off, so the portrait is not drawn.'
  }
}

export function CastPanel({ characters, settings, onClose, onCharacter, onMerged, onSample }: Props) {
  const portraits = settings !== null && settings.image_url !== ''
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
              {portraits && (
                <span className="cast-portrait" data-portrait={character.role === 'narrator' ? 'never' : character.portrait_state}>
                  {character.portrait_state === 'ready' && <img src={api.portraitUrl(character)} alt="" />}
                </span>
              )}
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
                looks={portraits && character.role !== 'narrator' ? { drawing: illustrating(settings) } : null}
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
  /** Null when this character has no portrait to show. `drawing` is false while pictures are switched off. */
  looks: { drawing: boolean } | null
  candidatePlaying: boolean
  onCandidate: () => void
  onMerged: (into: number) => void
}

function CharacterEditor({ character, others, onCharacter, looks, candidatePlaying, onCandidate, onMerged }: EditorProps) {
  const [draft, setDraft] = useState<CharacterInput>(() => inputOf(character))
  const [mergeInto, setMergeInto] = useState<number | ''>('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [finding, setFinding] = useState<'no' | 'asked' | 'reading' | { failed: string }>('no')
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

  const find = async () => {
    setFinding('reading')
    try {
      const found = await api.findAppearance(character.id)
      onCharacter(found)
      setDraft((current) => ({ ...current, appearance: found.appearance }))
      setFinding('no')
    } catch (error) {
      setFinding({ failed: error instanceof Error ? error.message : String(error) })
    }
  }
  const target = others.find((c) => c.id === mergeInto)

  return (
    <div className="cast-editor">
      <form onSubmit={save}>
        <CharacterFields
          value={draft}
          onChange={setDraft}
          appearance={
            looks
              ? {
                  action: (
                    <button type="button" className="field-action" disabled={finding !== 'no'} onClick={() => setFinding('asked')}>
                      Find in the book
                    </button>
                  ),
                }
              : undefined
          }
        />
        <div className="row-actions">
          <button type="submit" className="button" disabled={busy || !dirty || draft.name.trim() === ''}>
            Save changes
          </button>
        </div>
        <p className="candidate-note">
          The description shapes the next voice you try. The current voice stays until you replace it.
          {looks && ' A new appearance draws the portrait again.'}
        </p>
      </form>

      {finding !== 'no' && (
        <div className="candidate">
          {finding === 'reading' ? (
            <p className="candidate-note candidate-busy">Reading the book for how {character.name} looks…</p>
          ) : (
            <>
              {typeof finding === 'object' && <p className="cast-error cast-error-inline">Could not find it. {finding.failed}</p>}
              <p className="candidate-note">
                This makes one Claude or Codex call. It reads the whole book, including parts you have not read yet, so
                the result can reveal things from later chapters. It replaces the appearance above and draws the
                portrait again.
              </p>
              <div className="candidate-actions">
                <button className="button" onClick={() => void find()}>
                  {typeof finding === 'object' ? 'Try again' : 'Read the book'}
                </button>
                <button className="quiet-button" onClick={() => setFinding('no')}>
                  Cancel
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {looks && (
        <div className="cast-look">
          <span className="cast-portrait cast-portrait-large" data-portrait={character.portrait_state}>
            {character.portrait_state === 'ready' && <img src={api.portraitUrl(character)} alt={`Portrait of ${character.name}`} />}
          </span>
          <div>
            <p className="candidate-note" data-problem={character.portrait_state === 'failed' || undefined}>
              {portraitNote(character, looks.drawing)}
            </p>
            <button
              className="quiet-button"
              disabled={busy || character.appearance === ''}
              onClick={() => act(api.redrawPortrait)}
            >
              Draw again
            </button>
          </div>
        </div>
      )}

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
