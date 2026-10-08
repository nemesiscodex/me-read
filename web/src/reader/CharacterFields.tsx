import type { ReactNode } from 'react'
import type { CharacterInput } from '../api'
import { AGES, GENDERS, PITCHES } from '../lib/cast'

interface Props {
  value: CharacterInput
  onChange: (value: CharacterInput) => void
  autoFocus?: boolean
  /** Show the field for how the character looks, with this control beside its label. */
  appearance?: { action: ReactNode }
}

function Choice<T extends string>(props: { label: string; value: T; options: readonly T[]; onChange: (v: T) => void }) {
  return (
    <label className="field">
      <span>{props.label}</span>
      <select value={props.value} onChange={(event) => props.onChange(event.target.value as T)}>
        {props.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  )
}

export function CharacterFields({ value, onChange, autoFocus, appearance }: Props) {
  const set = (patch: Partial<CharacterInput>) => onChange({ ...value, ...patch })
  return (
    <div className="character-fields">
      <label className="field field-wide">
        <span>Name</span>
        <input
          type="text"
          value={value.name}
          autoFocus={autoFocus}
          onChange={(event) => set({ name: event.target.value })}
          required
        />
      </label>
      <Choice label="Gender" value={value.gender} options={GENDERS} onChange={(gender) => set({ gender })} />
      <Choice label="Age" value={value.age} options={AGES} onChange={(age) => set({ age })} />
      <Choice label="Pitch" value={value.pitch} options={PITCHES} onChange={(pitch) => set({ pitch })} />
      <label className="field field-wide">
        <span>Voice description</span>
        <textarea
          rows={2}
          value={value.description}
          placeholder="Gruff, slow, a little tired"
          onChange={(event) => set({ description: event.target.value })}
        />
      </label>
      {appearance && (
        <div className="field field-wide">
          <span className="field-head">
            <label htmlFor="character-appearance">Appearance</label>
            {appearance.action}
          </span>
          <textarea
            id="character-appearance"
            rows={2}
            maxLength={300}
            value={value.appearance}
            placeholder="Tall, grey beard, a worn blue coat"
            onChange={(event) => set({ appearance: event.target.value })}
          />
        </div>
      )}
    </div>
  )
}
