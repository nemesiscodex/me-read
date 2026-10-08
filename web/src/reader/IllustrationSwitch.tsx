import { api, type Settings } from '../api'

/** Turns looking for moments and drawing them on and off. Absent when there is no image service. */
export function IllustrationSwitch({ settings, onSettings }: { settings: Settings | null; onSettings: (settings: Settings) => void }) {
  if (!settings || settings.image_url === '') return null
  const on = settings.illustrations

  return (
    <button
      className="quiet-button tagger-switch"
      data-paused={!on || undefined}
      aria-pressed={on}
      title={
        on
          ? 'Key moments on this page and the next are drawn and shown as you reach them. Click to stop.'
          : 'No moments are looked for and nothing is drawn. Click to show pictures again.'
      }
      onClick={() => api.updateSettings({ illustrations: !on }).then(onSettings, () => {})}
    >
      {on ? 'Pictures on' : 'Pictures off'}
    </button>
  )
}
