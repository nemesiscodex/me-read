import type { Place } from './lib/pages'
import type { Block, BookDetail, BookSummary, Character, Emotion, Moment, ReusableVoice, Segment, Take } from './generated/server'

export * from './generated/server'

export type Gender = Character['gender']
export type Age = Character['age']
export type Pitch = Character['pitch']
export type VoiceState = Character['voice_state']
export type TagState = Block['tag_state']
export type TakeState = Take['state']
export type Provider = 'claude' | 'codex'
export const EMOTION_LABELS: Record<Emotion, string> = {
  neutral: 'Normal', happy: 'Happy', sad: 'Sad', angry: 'Angry (restrained)', scared: 'Scared', whisper: 'Whisper',
  yelling: 'Calling out', shouting: 'Shouting', angry_shouting: 'Angry shouting',
}

export interface Settings {
  provider: Provider
  claude_model: string
  codex_model: string
  /** While true the server makes no Claude or Codex calls, and the narrator reads lines that have no speaker yet. */
  tagging_paused: boolean
  /** Where the image service answers. Empty when there is none. */
  image_url: string
  /** The reader's switch. Moments are only looked for and drawn while it is on and `image_url` is set. */
  illustrations: boolean
  /** How many pictures of one page count, 1 to MAX_PICTURES_PER_PAGE. Starred pictures come on top. */
  pictures_per_page: number
}

export interface UsageTotals {
  calls: number
  paragraphs: number
  /** What the calls would cost at API prices, as the CLI reports it. Codex reports none. */
  cost_usd: number
}

export interface Usage {
  today: UsageTotals
  all_time: UsageTotals
}

export interface CharacterInput {
  name: string
  gender: Gender
  age: Age
  pitch: Pitch
  description: string
  appearance: string
}

/** Block positions (Block.idx) the reader is showing. The server prepares `first..last`, starting from `current_first`. */
export interface Focus {
  first: number
  last: number
  current_first: number
  current_last: number
}

export type ServerEvent =
  | { type: 'segments'; segments: Segment[] }
  | { type: 'blocks'; blocks: { id: number; tag_state: TagState }[] }
  | { type: 'characters'; characters: Character[] }
  | { type: 'moments'; moments: Moment[] }
  | { type: 'activity'; tagger: string | null; scenes: string | null; speech: string | null; images: string | null }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
})

function upload(file: File): RequestInit {
  const form = new FormData()
  form.append('file', file)
  return { method: 'POST', body: form }
}

export const api = {
  listBooks: () => request<BookSummary[]>('/api/books'),
  uploadBook: (file: File) => request<BookSummary>('/api/books', upload(file)),
  deleteBook: (id: number) => request<void>(`/api/books/${id}`, { method: 'DELETE' }),
  getBook: (id: number) => request<BookDetail>(`/api/books/${id}`),
  getChapter: (bookId: number, chapter: number) => request<Block[]>(`/api/books/${bookId}/chapters/${chapter}`),
  getMoments: (bookId: number, chapter: number) => request<Moment[]>(`/api/books/${bookId}/chapters/${chapter}/moments`),
  /** Draw the picture of a moment again with a new seed. */
  redrawMoment: (id: number) => request<Moment>(`/api/moments/${id}/redraw`, json('POST')),
  /** Have a picture of this paragraph drawn. One Claude or Codex call when the paragraph has no picture yet. */
  starBlock: (blockId: number) => request<Moment>(`/api/blocks/${blockId}/star`, json('POST')),
  /** `position` is saved as where to open the book next time. */
  setFocus: (bookId: number, focus: Focus, position: Place | null) =>
    request<void>(`/api/books/${bookId}/focus`, json('POST', { ...focus, position })),

  /** Assign a line to a character. Its audio is regenerated in the new voice. */
  setSpeaker: (segmentId: number, characterId: number) =>
    request<Segment>(`/api/segments/${segmentId}`, json('PATCH', { character_id: characterId })),
  /** Say how a spoken line is said. Its audio is regenerated in that emotion. */
  setEmotion: (segmentId: number, emotion: Emotion) =>
    request<Segment>(`/api/segments/${segmentId}`, json('PATCH', { emotion })),
  /** Take another reading of the same line with a new seed. */
  regenerateSegment: (segmentId: number) => request<Segment>(`/api/segments/${segmentId}/regenerate`, json('POST')),
  /** Ask the tagger to label a paragraph's spoken lines again. */
  retagBlock: (blockId: number) => request<void>(`/api/blocks/${blockId}/retag`, json('POST')),
  setWriter: (blockId: number, characterId: number) =>
    request<Segment[]>(`/api/blocks/${blockId}/writer`, json('POST', { character_id: characterId })),
  /** Ask the tagger to label the spoken lines of paragraphs `first` to `last` again. Returns how many paragraphs it will. */
  retagBlocks: (bookId: number, range: { first: number; last: number }) =>
    request<{ retagged: number }>(`/api/books/${bookId}/retag`, json('POST', range)),

  createCharacter: (bookId: number, input: CharacterInput) =>
    request<Character>(`/api/books/${bookId}/characters`, json('POST', input)),
  /** Saves the description. The voice only changes when a candidate is approved or a recording is uploaded; a new appearance redraws the portrait. */
  updateCharacter: (id: number, input: Partial<CharacterInput>) =>
    request<Character>(`/api/characters/${id}`, json('PATCH', input)),
  /** Design a candidate sample from the character's current description. Calling it again makes a different one. */
  requestDraft: (id: number) => request<Character>(`/api/characters/${id}/voice/draft`, json('POST')),
  /** Offer the current voice moved up or down by whole semitones as the candidate, in place of any being designed. */
  shiftVoice: (id: number, semitones: number) =>
    request<Character>(`/api/characters/${id}/voice/shift`, json('POST', { semitones })),
  discardDraft: (id: number) => request<Character>(`/api/characters/${id}/voice/draft`, { method: 'DELETE' }),
  /** Make the candidate the character's voice. Their lines are read again in it. */
  approveDraft: (id: number) => request<Character>(`/api/characters/${id}/voice/draft/approve`, json('POST')),
  uploadVoice: (id: number, file: File) => request<Character>(`/api/characters/${id}/voice`, upload(file)),
  listVoices: () => request<ReusableVoice[]>('/api/voices'),
  reuseVoice: (id: number, voice: ReusableVoice) => request<Character>(`/api/characters/${id}/voice/reuse`,
    json('POST', { source_id: voice.id, source_rev: voice.voice_rev })),
  /** Make the take now, or make it again. Lines already read in it are read again in the new take. */
  makeTake: (id: number, emotion: Take['emotion']) =>
    request<Character>(`/api/characters/${id}/takes/${emotion}`, json('POST')),
  /** Lines already read in the take are read again when this changes. 0 reads it as it is. */
  setTakeSemitones: (id: number, emotion: Take['emotion'], semitones: number) =>
    request<Character>(`/api/characters/${id}/takes/${emotion}/semitones`, json('PUT', { semitones })),
  /** Draw the portrait again with a new seed, and the pictures the character is in. */
  redrawPortrait: (id: number) => request<Character>(`/api/characters/${id}/portrait/redraw`, json('POST')),
  /** One Claude or Codex call over every paragraph of the book about the character. Replaces the appearance. */
  findAppearance: (id: number) => request<Character>(`/api/characters/${id}/appearance/find`, json('POST')),
  /** Move every line of `id` to `into`, then delete `id`. */
  mergeCharacter: (id: number, into: number) => request<void>(`/api/characters/${id}/merge`, json('POST', { into })),

  getSettings: () => request<Settings>('/api/settings'),
  updateSettings: (settings: Partial<Settings>) => request<Settings>('/api/settings', json('PUT', settings)),
  getUsage: () => request<Usage>('/api/usage'),

  audioUrl: (segment: Segment) => `/api/segments/${segment.id}/audio?v=${segment.audio_rev}.${segment.leveled}`,
  voiceUrl: (character: Pick<Character, 'id' | 'voice_rev'>) => `/api/characters/${character.id}/voice?v=${character.voice_rev}`,
  takeUrl: (character: Character, take: Take) =>
    `/api/characters/${character.id}/takes/${take.emotion}/audio?v=${character.voice_rev}.${take.rev}`,
  draftUrl: (character: Character) => `/api/characters/${character.id}/voice/draft?v=${character.draft_rev}`,
  portraitUrl: (character: Character) => `/api/characters/${character.id}/portrait?v=${character.portrait_rev}`,
  momentUrl: (moment: Moment) => `/api/moments/${moment.id}/image?v=${moment.image_rev}`,
  /** Server-sent stream of ServerEvent for one book. */
  events: (bookId: number) => new EventSource(`/api/books/${bookId}/events`),
}
