export type Gender = 'male' | 'female' | 'unknown'
export type Age = 'child' | 'teen' | 'adult' | 'elderly'
export type Pitch = 'low' | 'medium' | 'high'
export type Role = 'narrator' | 'named' | 'extra'
export type VoiceState = 'none' | 'designing' | 'ready' | 'failed'
export type TagState = 'pending' | 'tagging' | 'tagged' | 'failed'
export type AudioState = 'none' | 'generating' | 'ready' | 'failed'
export type DraftState = 'none' | 'queued' | 'designing' | 'ready' | 'failed'
export type Provider = 'claude' | 'codex'

export interface Character {
  id: number
  book_id: number
  name: string
  role: Role
  gender: Gender
  age: Age
  pitch: Pitch
  description: string
  voice_state: VoiceState
  /** Bumped whenever the 15 second sample changes; use it to bust the audio cache. */
  voice_rev: number
  error: string | null
  /** A candidate voice sample being auditioned. The current voice stays in use until the candidate is approved. */
  draft_state: DraftState
  draft_rev: number
  line_count: number
}

export interface Segment {
  id: number
  block_id: number
  seq: number
  /** Raw text. A block's segments concatenate to exactly the block's text. */
  text: string
  kind: 'narration' | 'speech'
  /** null while a spoken line is still waiting for the tagger. */
  character_id: number | null
  audio_state: AudioState
  audio_rev: number
  duration: number | null
  error: string | null
}

export interface Block {
  id: number
  /** Position in the whole book, zero based. */
  idx: number
  chapter: number
  tag: 'p' | 'h'
  tag_state: TagState
  segments: Segment[]
}

export interface Chapter {
  index: number
  title: string
  first_block: number
  block_count: number
}

export interface BookSummary {
  id: number
  title: string
  author: string
  block_count: number
  position_block: number
  /** Two-letter code detected from the text: 'en' or 'es'. It picks the speech model and the sample voices' language. */
  language: string
}

export interface BookDetail extends BookSummary {
  chapters: Chapter[]
  characters: Character[]
}

export interface Settings {
  provider: Provider
  claude_model: string
  codex_model: string
  /** While true the server makes no Claude or Codex calls, and the narrator reads lines that have no speaker yet. */
  tagging_paused: boolean
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
  | { type: 'activity'; tagger: string | null; speech: string | null }

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
  setFocus: (bookId: number, focus: Focus) => request<void>(`/api/books/${bookId}/focus`, json('POST', focus)),

  /** Assign a line to a character. Its audio is regenerated in the new voice. */
  setSpeaker: (segmentId: number, characterId: number) =>
    request<Segment>(`/api/segments/${segmentId}`, json('PATCH', { character_id: characterId })),
  /** Take another reading of the same line with a new seed. */
  regenerateSegment: (segmentId: number) => request<Segment>(`/api/segments/${segmentId}/regenerate`, json('POST')),
  /** Ask the tagger to label a paragraph's spoken lines again. */
  retagBlock: (blockId: number) => request<void>(`/api/blocks/${blockId}/retag`, json('POST')),

  createCharacter: (bookId: number, input: CharacterInput) =>
    request<Character>(`/api/books/${bookId}/characters`, json('POST', input)),
  /** Saves the description. The voice only changes when a candidate is approved or a recording is uploaded. */
  updateCharacter: (id: number, input: Partial<CharacterInput>) =>
    request<Character>(`/api/characters/${id}`, json('PATCH', input)),
  /** Design a candidate sample from the character's current description. Calling it again makes a different one. */
  requestDraft: (id: number) => request<Character>(`/api/characters/${id}/voice/draft`, json('POST')),
  discardDraft: (id: number) => request<Character>(`/api/characters/${id}/voice/draft`, { method: 'DELETE' }),
  /** Make the candidate the character's voice. Their lines are read again in it. */
  approveDraft: (id: number) => request<Character>(`/api/characters/${id}/voice/draft/approve`, json('POST')),
  uploadVoice: (id: number, file: File) => request<Character>(`/api/characters/${id}/voice`, upload(file)),
  /** Move every line of `id` to `into`, then delete `id`. */
  mergeCharacter: (id: number, into: number) => request<void>(`/api/characters/${id}/merge`, json('POST', { into })),

  getSettings: () => request<Settings>('/api/settings'),
  updateSettings: (settings: Partial<Settings>) => request<Settings>('/api/settings', json('PUT', settings)),
  getUsage: () => request<Usage>('/api/usage'),

  audioUrl: (segment: Segment) => `/api/segments/${segment.id}/audio?v=${segment.audio_rev}`,
  voiceUrl: (character: Character) => `/api/characters/${character.id}/voice?v=${character.voice_rev}`,
  draftUrl: (character: Character) => `/api/characters/${character.id}/voice/draft?v=${character.draft_rev}`,
  /** Server-sent stream of ServerEvent for one book. */
  events: (bookId: number) => new EventSource(`/api/books/${bookId}/events`),
}
