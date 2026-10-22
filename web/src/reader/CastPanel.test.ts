// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type Character } from '../api'
import { character, take } from '../lib/fixtures'
import { CastPanel } from './CastPanel'

let host: HTMLDivElement
let root: Root

async function render(candidate: Character, speechActivity: string | null = null) {
  await act(async () => root.render(createElement(CastPanel, {
    characters: [candidate], settings: null, emotions: false, speechActivity,
    onClose() {}, onCharacter() {}, onMerged() {}, onSample() {},
  })))
  const summary = host.querySelector<HTMLButtonElement>('.cast-summary')
  expect(summary).not.toBeNull()
  if (summary?.getAttribute('aria-expanded') === 'false') await act(async () => summary.click())
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-18T20:00:00Z'))
  host = document.createElement('div')
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  expect(vi.getTimerCount()).toBe(0)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('distinguishes a queued candidate from a voice that is being designed', async () => {
  await render(character(98, { name: 'Professor Reed', draft_state: 'queued' }), "Directing Owen's voice: happy")
  expect(host.querySelector('.candidate')?.textContent).toContain('Waiting for the speech worker')
  expect(host.querySelector('.candidate')?.textContent).not.toContain('Designing a candidate voice')
  expect(host.querySelector('.candidate')?.textContent).toContain("Directing Owen's voice: happy")
})

it('shows the latest real step, elapsed time, and a long wait without claiming completion', async () => {
  const candidate = character(98, {
    draft_state: 'designing', draft_status: 'Generating voice sample (attempt 1)…', draft_updated_at: Date.now() / 1000,
  })
  await render(candidate)
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Generating voice sample (attempt 1)')
  await act(async () => vi.advanceTimersByTime(65000))
  expect(host.querySelector('.candidate-timing')?.textContent).toContain('1m 5s ago')
  expect(host.querySelector('.candidate-timing')?.textContent).toContain('completion has not been confirmed')
  await render({ ...candidate, draft_status: 'Comparing with saved voice 2 of 8…', draft_updated_at: Date.now() / 1000 })
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Comparing with saved voice 2 of 8')
  expect(host.querySelector('.candidate-timing')?.textContent).toContain('0s ago')
  expect(host.querySelector('.candidate-timing')?.textContent).not.toContain('completion has not been confirmed')
  await render({ ...candidate, draft_state: 'ready', draft_status: null })
  expect(host.querySelector('.candidate')?.textContent).toContain('A candidate voice is ready')
  expect(host.querySelector('.candidate-timing')).toBeNull()
})

it('shows the service outage and automatic retry rather than pretending to design', async () => {
  await render(character(98, { draft_state: 'queued', draft_status: 'Speech service unavailable. Retrying automatically.' }))
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Speech service unavailable. Retrying automatically.')
})

it('shows a failed design reason and leaves retry available', async () => {
  await render(character(98, { draft_state: 'failed', draft_status: 'The designed voice sample came out too short' }))
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('The designed voice sample came out too short')
  expect([...host.querySelectorAll('button')].some(b => b.textContent === 'Try a new voice' && !b.disabled)).toBe(true)
})

it('lets the reader cancel a candidate that is waiting for progress', async () => {
  const discard = vi.spyOn(api, 'discardDraft').mockResolvedValue(character(98))
  await render(character(98, { draft_state: 'designing', draft_status: 'Comparing with saved voice 2 of 8…' }))
  const cancel = [...host.querySelectorAll('button')].find(b => b.textContent === 'Cancel')
  expect(cancel).toBeDefined()
  await act(async () => cancel?.click())
  expect(discard).toHaveBeenCalledWith(98)
})

it('applies a saved voice from the editor and closes the picker after success', async () => {
  const voice = { ...character(12), book_title: 'Another Book', book_author: 'A. Writer', language: 'en' }
  vi.spyOn(api, 'listVoices').mockResolvedValue([voice])
  const reuse = vi.spyOn(api, 'reuseVoice').mockResolvedValue(character(98, { voice_rev: 2 }))
  await render(character(98))
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === 'Reuse a voice…')?.click())
  expect(host.querySelector('.voice-picker')).not.toBeNull()
  expect(reuse).not.toHaveBeenCalled()
  await act(async () => host.querySelector<HTMLButtonElement>('.saved-voice .button')?.click())
  expect(reuse).toHaveBeenCalledWith(98, voice)
  expect(host.querySelector('.voice-picker')).toBeNull()
})

it('keeps the picker available and reports a failed replacement', async () => {
  const voice = { ...character(12), book_title: 'Another Book', book_author: 'A. Writer', language: 'en' }
  vi.spyOn(api, 'listVoices').mockResolvedValue([voice])
  vi.spyOn(api, 'reuseVoice').mockRejectedValue(new Error('That saved voice changed; reopen the voice list'))
  await render(character(98))
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === 'Reuse a voice…')?.click())
  await act(async () => host.querySelector<HTMLButtonElement>('.saved-voice .button')?.click())
  expect(host.querySelector('.voice-picker')).not.toBeNull()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('That saved voice changed')
  expect(host.querySelector<HTMLButtonElement>('.saved-voice .button')?.disabled).toBe(false)
})

it('makes a candidate from the current voice moved by the chosen semitones', async () => {
  const shift = vi.spyOn(api, 'shiftVoice').mockResolvedValue(character(98, { draft_state: 'ready', draft_rev: 1 }))
  await render(character(98))
  const button = (label: string) =>
    [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === label || b.getAttribute('aria-label') === label)
  expect(host.querySelector('.voice-shift output')?.textContent).toBe('0')
  expect(button('Make candidate')?.disabled).toBe(true)
  await act(async () => button('One semitone higher')?.click())
  await act(async () => button('One semitone higher')?.click())
  expect(host.querySelector('.voice-shift output')?.textContent).toBe('+2')
  await act(async () => button('Make candidate')?.click())
  expect(shift).toHaveBeenCalledWith(98, 2)
})

it('offers no pitch shift before the character has a voice', async () => {
  await render(character(98, { voice_state: 'designing' }))
  expect(host.querySelector('.voice-shift')).toBeNull()
})

it('reads a take the chosen semitones lower and raises it back', async () => {
  const set = vi.spyOn(api, 'setTakeSemitones').mockResolvedValue(character(98))
  const takes = [take('angry', { state: 'ready' }), take('shouting', { state: 'ready', semitones: -2 })]
  await act(async () => root.render(createElement(CastPanel, {
    characters: [character(98, { name: 'Owen', takes })], settings: null, emotions: true, speechActivity: null,
    onClose() {}, onCharacter() {}, onMerged() {}, onSample() {},
  })))
  const shift = (label: string) => host.querySelector<HTMLSelectElement>(`select[aria-label="Semitones for Owen, ${label}"]`)!
  expect(shift('Angry (restrained)').value).toBe('0')
  expect(shift('Shouting').selectedOptions[0]?.textContent).toBe('-2')
  await act(async () => {
    shift('Shouting').value = '-3'
    shift('Shouting').dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(set).toHaveBeenCalledWith(98, 'shouting', -3)
})
