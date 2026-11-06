// Written by server/meread/web_types.py from server/meread/store.py and emotions.py. Do not edit.
// Regenerate with: cd server && uv run python -m meread.web_types

export const MAX_PICTURES_PER_PAGE = 12

export type Emotion = 'neutral' | 'happy' | 'sad' | 'angry' | 'scared' | 'whisper' | 'yelling' | 'shouting' | 'angry_shouting'

export interface BookSummary {
  id: number
  title: string
  author: string
  block_count: number
  position_block: number
  /** With position_block, the line at the top of the page last read: segment.seq in that block. */
  position_seq: number
  /** Two-letter code detected from the text: 'en' or 'es'. It picks the speech model and the sample voices' language. */
  language: string
}

export interface BookDetail {
  id: number
  title: string
  author: string
  block_count: number
  position_block: number
  /** With position_block, the line at the top of the page last read: segment.seq in that block. */
  position_seq: number
  /** Two-letter code detected from the text: 'en' or 'es'. It picks the speech model and the sample voices' language. */
  language: string
  /** Delivery options supported by this book's language and the selected narrator. */
  emotions: Emotion[]
  chapters: Chapter[]
  characters: Character[]
}

export interface Chapter {
  index: number
  title: string
  first_block: number
  block_count: number
}

export interface Block {
  id: number
  /** Position in the whole book, zero based. */
  idx: number
  chapter: number
  tag: 'p' | 'h'
  tag_state: 'pending' | 'tagging' | 'tagged' | 'failed'
  /** Who wrote the paragraph, such as a letter, when it is read in their voice. NULL otherwise. */
  writer_id: number | null
  segments: Segment[]
}

export interface Segment {
  id: number
  block_id: number
  seq: number
  /** Raw text. A block's segments concatenate to exactly the block's text. */
  text: string
  kind: 'narration' | 'speech'
  /** NULL while a spoken line is still waiting for the tagger. */
  character_id: number | null
  /** How a spoken line is said. Narration is always neutral. */
  emotion: Emotion
  audio_state: 'none' | 'generating' | 'ready' | 'failed'
  audio_rev: number
  duration: number | null
  error: string | null
  /** 1 once the clip on disk is at the speech level all clips share. Clips made before that are leveled later, which changes the file, not audio_rev. */
  leveled: number
}

export interface Character {
  id: number
  book_id: number
  name: string
  role: 'narrator' | 'named' | 'extra'
  gender: 'male' | 'female' | 'unknown'
  age: 'child' | 'teen' | 'adult' | 'elderly'
  pitch: 'low' | 'medium' | 'high'
  description: string
  voice_state: 'none' | 'designing' | 'ready' | 'failed'
  /** Bumped whenever the 15 second sample changes; use it to bust the audio cache. */
  voice_rev: number
  error: string | null
  /** A candidate voice sample being auditioned. The current voice stays in use until the candidate is approved. */
  draft_state: 'none' | 'queued' | 'designing' | 'ready' | 'failed'
  draft_rev: number
  /** The current candidate step or failure reason. */
  draft_status: string | null
  /** When draft_status last changed, in server time (Unix seconds). */
  draft_updated_at: number | null
  /** How the character looks, in words. The portrait is drawn from it, and drawn again whenever it changes. */
  appearance: string
  portrait_state: 'none' | 'drawing' | 'ready' | 'failed'
  portrait_rev: number
  portrait_error: string | null
  line_count: number
  /** One for each emotion other than neutral, in the order of Emotion. Neutral is the voice itself. */
  takes: Take[]
}

/** The character's voice directed into an emotion. Lines in that emotion are read from it once it is ready. */
export interface Take {
  emotion: 'happy' | 'sad' | 'angry' | 'scared' | 'whisper' | 'yelling' | 'shouting' | 'angry_shouting'
  /** 'none' also for a take made from a voice the character no longer has. It is made again when a line needs it. */
  state: 'none' | 'queued' | 'making' | 'ready' | 'failed'
  /** Bumped each time the take is made again by hand. */
  rev: number
  /** Lines are cloned from the take moved by this many semitones, then moved back. Below 0 for a take too high to clone. */
  semitones: number
}

export interface ReusableVoice {
  id: number
  book_id: number
  name: string
  gender: 'male' | 'female' | 'unknown'
  age: 'child' | 'teen' | 'adult' | 'elderly'
  pitch: 'low' | 'medium' | 'high'
  description: string
  /** Bumped whenever the 15 second sample changes; use it to bust the audio cache. */
  voice_rev: number
  book_title: string
  book_author: string
  /** Two-letter code detected from the text: 'en' or 'es'. It picks the speech model and the sample voices' language. */
  language: string
}

/** A paragraph where the picture changes: a new one is shown, or with kind 'clear' the last one is taken down. */
export interface Moment {
  id: number
  block_id: number
  /** block.idx of the paragraph. */
  block_idx: number
  kind: 'picture' | 'clear'
  /** A few words to put under the picture. Empty for a clear, and for a moment found before titles existed. */
  title: string
  /** One sentence on what happens. */
  summary: string
  /** What the picture is drawn from. Empty for a clear. */
  scene: string
  /** 1 to 10. Of the moments on a page the highest count first. */
  importance: number
  image_state: 'none' | 'drawing' | 'ready' | 'failed'
  image_rev: number
  error: string | null
  /** The reader asked for this picture by hand. It is drawn wherever it is and takes none of its page's places. */
  starred: boolean
  /** The characters in the picture, most important first. */
  character_ids: number[]
}
