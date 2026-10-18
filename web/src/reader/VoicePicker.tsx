import { useEffect, useRef, useState } from 'react'
import { api, type Character, type ReusableVoice } from '../api'
import { Icon } from '../Icon'

interface Props {
  character: Character
  busy: boolean
  playingId: number | null
  onListen: (voice: ReusableVoice) => void
  onUse: (voice: ReusableVoice) => void
  onClose: () => void
}

type VoiceList = { state: 'loading' } | { state: 'ready'; voices: ReusableVoice[] } | { state: 'failed'; message: string }

export function VoicePicker({ character, busy, playingId, onListen, onUse, onClose }: Props) {
  const [list, setList] = useState<VoiceList>({ state: 'loading' })
  const [query, setQuery] = useState('')
  const [attempt, setAttempt] = useState(0)
  const panel = useRef<HTMLElement>(null)

  useEffect(() => {
    let cancelled = false
    setList({ state: 'loading' })
    api.listVoices().then(
      (voices) => { if (!cancelled) setList({ state: 'ready', voices }) },
      (error: unknown) => { if (!cancelled) setList({ state: 'failed', message: error instanceof Error ? error.message : String(error) }) },
    )
    return () => { cancelled = true }
  }, [attempt])

  useEffect(() => {
    if (list.state === 'ready') panel.current?.scrollIntoView({ block: 'start' })
  }, [list.state])

  const voices = list.state === 'ready' ? list.voices.filter((voice) => voice.id !== character.id) : []
  const words = query.trim().toLocaleLowerCase().split(/\s+/)
  const shown = voices.filter((voice) => {
    const text = `${voice.name} ${voice.book_title} ${voice.book_author} ${voice.description}`.toLocaleLowerCase()
    return words.every((word) => text.includes(word))
  }).sort((a, b) => Number(b.name.toLocaleLowerCase() === character.name.toLocaleLowerCase())
    - Number(a.name.toLocaleLowerCase() === character.name.toLocaleLowerCase()))

  return (
    <section ref={panel} className="candidate voice-picker" aria-label="Reuse a voice" onKeyDown={(event) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      }
    }}>
      <div className="voice-picker-heading">
        <h3>Reuse a voice</h3>
        <button className="icon-button" aria-label="Close saved voices" onClick={onClose}><Icon name="close" /></button>
      </div>
      <p className="candidate-note">
        Voices from all your books. Listen before choosing. Using one reads {character.name}’s lines again;
        the original character keeps their voice.
      </p>
      <input type="search" className="cast-search" placeholder="Character or book" aria-label="Search saved voices"
        autoFocus value={query} onChange={(event) => setQuery(event.target.value)} />
      {list.state === 'loading' && <p className="candidate-note" role="status">Loading saved voices…</p>}
      {list.state === 'failed' && <div role="alert">
        <p className="cast-error cast-error-inline">Could not load saved voices. {list.message}</p>
        <button className="quiet-button" onClick={() => setAttempt((current) => current + 1)}>Try again</button>
      </div>}
      {list.state === 'ready' && (voices.length === 0 ? (
        <p className="candidate-note">No other saved voices yet. Voices appear here once another character’s voice is ready.</p>
      ) : shown.length === 0 ? (
        <p className="candidate-note">No saved voices match “{query.trim()}”.</p>
      ) : (
        <ul className="saved-voice-list" aria-label="Saved voices">
          {shown.map((voice) => (
            <li key={voice.id} className="saved-voice">
              <span className="cast-name">{voice.name}</span>
              <span className="cast-meta">{voice.book_title}{voice.book_id === character.book_id && ' · This book'}</span>
              <span className="cast-meta">{voice.book_author} · {voice.language}</span>
              {voice.description && <p className="candidate-note">{voice.description}</p>}
              <div className="candidate-actions">
                <button className="quiet-button" onClick={() => onListen(voice)}
                  aria-label={`${playingId === voice.id ? 'Stop' : 'Hear'} ${voice.name} from ${voice.book_title}`}>
                  <Icon name={playingId === voice.id ? 'pause' : 'play'} /> {playingId === voice.id ? 'Stop' : 'Listen'}
                </button>
                <button className="button" disabled={busy} onClick={() => onUse(voice)}
                  aria-label={`Use ${voice.name}'s voice from ${voice.book_title}`}>Use this voice</button>
              </div>
            </li>
          ))}
        </ul>
      ))}
    </section>
  )
}
