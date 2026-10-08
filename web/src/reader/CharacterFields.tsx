import type { CharacterInput } from '../api'
import { AGES, GENDERS, PITCHES } from '../lib/cast'

interface Props {
  value: CharacterInput
  onChange: (value: CharacterInput) => void
  autoFocus?: boolean
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

export function CharacterFields({ value, onChange, autoFocus }: Props) {
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
    </div>
  )
}
