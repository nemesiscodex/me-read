import type { Age, Character, CharacterInput, Gender, Pitch } from '../api'

export const GENDERS: readonly Gender[] = ['female', 'male', 'unknown']
export const AGES: readonly Age[] = ['child', 'teen', 'adult', 'elderly']
export const PITCHES: readonly Pitch[] = ['low', 'medium', 'high']

export const EMPTY_CHARACTER: CharacterInput = {
  name: '',
  gender: 'unknown',
  age: 'adult',
  pitch: 'medium',
  description: '',
}

const GOLDEN_ANGLE = 137.508

/** Hue in degrees. Consecutive ids land far apart on the wheel, and an id always maps to the same hue. */
export function characterHue(id: number): number {
  return Math.round(((Math.abs(id) * GOLDEN_ANGLE) % 360) * 10) / 10
}

export function narratorOf(characters: Character[]): Character | null {
  return characters.find((c) => c.role === 'narrator') ?? null
}

const ROLE_ORDER = { narrator: 0, named: 1, extra: 2 } as const

/** Narrator first, then named characters before extras, busiest first. */
export function sortCast(characters: Character[]): Character[] {
  return [...characters].sort(
    (a, b) =>
      ROLE_ORDER[a.role] - ROLE_ORDER[b.role] || b.line_count - a.line_count || a.name.localeCompare(b.name),
  )
}

export function inputOf(character: Character): CharacterInput {
  const { name, gender, age, pitch, description } = character
  return { name, gender, age, pitch, description }
}

/** Only the fields that differ, so an untouched voice field never triggers a redesign. */
export function changedFields(before: CharacterInput, after: CharacterInput): Partial<CharacterInput> {
  const changed: Partial<CharacterInput> = {}
  for (const key of Object.keys(after) as (keyof CharacterInput)[]) {
    if (before[key] !== after[key]) Object.assign(changed, { [key]: after[key] })
  }
  return changed
}
