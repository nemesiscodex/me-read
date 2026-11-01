// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { api } from '../api'
import { block, character, segment } from '../lib/fixtures'
import { CorrectionPopover } from './CorrectionPopover'

it('assigns every segment when the reader chooses a voice for the whole paragraph', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.createElement('div')
  const root = createRoot(host)
  const paragraph = block(7, [segment(1), segment(2)])
  const saved = paragraph.segments.map((s) => ({ ...s, character_id: 2 }))
  const setWriter = vi.spyOn(api, 'setWriter').mockResolvedValue(saved)
  const setSpeaker = vi.spyOn(api, 'setSpeaker')
  const onSegment = vi.fn()
  const onClose = vi.fn()
  try {
    await act(async () => root.render(createElement(CorrectionPopover, {
      bookId: 1, segment: paragraph.segments[0]!, block: paragraph,
      characters: [character(2, { name: 'Nora' })], at: { x: 0, y: 0 },
      pages: { current: null, next: null }, canStar: false, emotions: ['neutral'],
      onClose, onSegment, onCharacter() {}, onMoment() {},
    })))
    const checkbox = host.querySelector<HTMLInputElement>('input[type="checkbox"]')
    expect(checkbox).not.toBeNull()
    await act(async () => checkbox?.click())
    const speaker = host.querySelector<HTMLButtonElement>('.speaker-option')
    expect(speaker?.textContent).toContain('Nora')
    await act(async () => speaker?.click())
    expect(setWriter).toHaveBeenCalledWith(7, 2)
    expect(setSpeaker).not.toHaveBeenCalled()
    expect(onSegment.mock.calls.map(([s]) => s)).toEqual(saved)
    expect(onClose).toHaveBeenCalledOnce()
  } finally {
    await act(async () => root.unmount())
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  }
})
