import { useEffect, useState } from 'react'
import { api, type Settings, type Usage } from '../api'

const PROVIDER_NAME = { claude: 'Claude', codex: 'Codex' }

/** Turns background Claude or Codex calls on and off, and shows how many were made today. */
export function TaggerSwitch({ activity }: { activity: string | null }) {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [usage, setUsage] = useState<Usage | null>(null)

  useEffect(() => {
    api.getSettings().then(setSettings, () => {})
  }, [])

  // The tagger's status line changes when a call starts and when it ends.
  useEffect(() => {
    api.getUsage().then(setUsage, () => {})
  }, [activity])

  if (!settings) return null
  const paused = settings.tagging_paused
  const name = PROVIDER_NAME[settings.provider]
  const today = usage?.today
  const spent = today && today.cost_usd > 0 ? `, about $${today.cost_usd.toFixed(2)} at API prices` : ''
  const count = today ? `${today.calls} ${today.calls === 1 ? 'call' : 'calls'} today` : ''

  return (
    <button
      className="quiet-button tagger-switch"
      data-paused={paused || undefined}
      aria-pressed={!paused}
      title={
        paused
          ? `${name} is not being called. The narrator reads lines that have no speaker yet. ${count}${spent}.`
          : `${name} decides who says each line on the pages around you. ${count}${spent}. Click to stop calling it.`
      }
      onClick={() => api.updateSettings({ tagging_paused: !paused }).then(setSettings, () => {})}
    >
      {paused ? `${name} paused` : `${name} on`}
      {today && <span className="tagger-count"> · {today.calls}</span>}
    </button>
  )
}
