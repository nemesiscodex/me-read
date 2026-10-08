import type { Character } from '../api'
import { Icon } from '../Icon'
import type { PlayerState } from '../lib/player'
import type { Pauses } from '../lib/pauses'
import { SPEED } from '../lib/prefs'
import { hueStyle } from './BlockView'
import { PauseSettings } from './PauseSettings'

interface Props {
  state: PlayerState
  speaker: Character | null
  canStart: boolean
  rate: number
  pauses: Pauses
  onPauses: (pauses: Pauses) => void
  onToggle: () => void
  onSkip: (dir: 1 | -1) => void
  onRate: (rate: number) => void
  onCorrect: (anchor: HTMLElement) => void
}

const STATUS_NOTE: Record<PlayerState['status'], string | null> = {
  idle: 'Click any line to listen from there',
  playing: null,
  paused: 'paused',
  waiting: 'preparing…',
  turning: 'Opening the next chapter…',
}

export function PlayerBar({ state, speaker, canStart, rate, pauses, onPauses, onToggle, onSkip, onRate, onCorrect }: Props) {
  const hasSegment = 'segmentId' in state
  const running = state.status === 'playing' || state.status === 'waiting' || state.status === 'turning'
  const note = STATUS_NOTE[state.status]

  return (
    <div className="player">
      <div className="player-buttons">
        <button className="icon-button" onClick={() => onSkip(-1)} disabled={!hasSegment} aria-label="Previous line">
          <Icon name="previous" />
        </button>
        <button
          className="icon-button play-button"
          onClick={onToggle}
          disabled={!hasSegment && !canStart && state.status !== 'turning'}
          aria-label={running ? 'Pause' : 'Play'}
        >
          <Icon name={running ? 'pause' : 'play'} size={22} />
        </button>
        <button className="icon-button" onClick={() => onSkip(1)} disabled={!hasSegment} aria-label="Next line">
          <Icon name="next" />
        </button>
      </div>

      <div className="player-now" aria-live="polite">
        {hasSegment && speaker && (
          <>
            {speaker.role !== 'narrator' && <span className="swatch" style={hueStyle(speaker.id)} />}
            <span className="player-speaker">{speaker.name}</span>
          </>
        )}
        {hasSegment && !speaker && <span className="player-speaker">Unknown speaker</span>}
        {note && <span className="player-note" data-busy={state.status === 'waiting' || undefined}>{note}</span>}
        {hasSegment && (
          <button
            className="icon-button"
            onClick={(event) => onCorrect(event.currentTarget)}
            aria-label="Correct this line"
            title="Correct this line"
          >
            <Icon name="more" />
          </button>
        )}
      </div>

      <label className="speed">
        <span className="speed-value">{rate.toFixed(2).replace(/0$/, '')}×</span>
        <input
          type="range"
          min={SPEED.min}
          max={SPEED.max}
          step={0.05}
          value={rate}
          onChange={(event) => onRate(Number(event.target.value))}
          aria-label="Reading speed"
        />
      </label>
      <PauseSettings value={pauses} onChange={onPauses} />
    </div>
  )
}
