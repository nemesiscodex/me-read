// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type ReusableVoice } from '../api'
import { character } from '../lib/fixtures'
import { VoicePicker } from './VoicePicker'

let host: HTMLDivElement
let root: Root
const target = character(98, { name: 'Professor Reed' })
const voices: ReusableVoice[] = [
  { ...character(2, { name: 'Anna' }), book_title: 'The Gate', book_author: 'A. Writer', language: 'en' },
  { ...character(3, { name: 'Professor Reed', book_id: 2 }), book_title: 'The First Adventure', book_author: 'Another Writer', language: 'en' },
  { ...target, book_title: 'This Adventure', book_author: 'A. Writer', language: 'en' },
]
const onListen = vi.fn()
const onUse = vi.fn()
const onClose = vi.fn()

async function render(busy = false, playingId: number | null = null) {
  await act(async () => root.render(createElement(VoicePicker, { character: target, busy, playingId, onListen, onUse, onClose })))
}

async function search(query: string) {
  const input = host.querySelector('input')
  if (!input) throw new Error('Missing voice search')
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, query)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  vi.spyOn(api, 'listVoices').mockResolvedValue(voices)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

it('prioritizes the same character across books and excludes the destination voice', async () => {
  await render()
  const names = [...host.querySelectorAll('.saved-voice .cast-name')].map((el) => el.textContent)
  expect(names).toEqual(['Professor Reed', 'Anna'])
  expect(host.textContent).toContain('The First Adventure')
  expect(host.textContent).toContain('This book')
  expect(host.textContent).not.toContain('This Adventure')
})

it('searches by character, book and author, and reports no matches', async () => {
  await render()
  for (const query of ['professor reed', 'first adventure', 'another writer']) {
    await search(query)
    expect(host.querySelectorAll('.saved-voice')).toHaveLength(1)
    expect(host.querySelector('.saved-voice')?.textContent).toContain('Professor Reed')
  }
  await search('missing')
  expect(host.textContent).toContain('No saved voices match “missing”')
  await search('')
  expect(host.querySelectorAll('.saved-voice')).toHaveLength(2)
})

it('previews without applying and disables replacement during a request', async () => {
  await render(false, 3)
  const listen = host.querySelector<HTMLButtonElement>('button[aria-label="Stop Professor Reed from The First Adventure"]')
  await act(async () => listen?.click())
  expect(onListen).toHaveBeenCalledWith(voices[1])
  expect(onUse).not.toHaveBeenCalled()
  const use = host.querySelector<HTMLButtonElement>('.saved-voice .button')
  await act(async () => use?.click())
  expect(onUse).toHaveBeenCalledWith(voices[1])
  await render(true)
  expect([...host.querySelectorAll<HTMLButtonElement>('.saved-voice .button')].every((b) => b.disabled)).toBe(true)
})

it('shows loading, allows retry after failure, and explains an empty library', async () => {
  let fail: (reason: Error) => void = () => {}
  vi.mocked(api.listVoices).mockReturnValueOnce(new Promise((_, reject) => { fail = reject }))
  await render()
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Loading saved voices')
  await act(async () => fail(new Error('Connection lost')))
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('Connection lost')
  vi.mocked(api.listVoices).mockResolvedValueOnce([voices[2]])
  const retry = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Try again')
  await act(async () => retry?.click())
  expect(host.textContent).toContain('No other saved voices yet')
})

it('closes on Escape without closing the Cast drawer', async () => {
  await render()
  const escaped = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })
  const outer = vi.fn()
  window.addEventListener('keydown', outer)
  try {
    await act(async () => host.querySelector('input')?.dispatchEvent(escaped))
    expect(onClose).toHaveBeenCalledOnce()
    expect(outer).not.toHaveBeenCalled()
  } finally {
    window.removeEventListener('keydown', outer)
  }
})
