import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, type Provider, type Settings } from '../api'

const PROVIDERS: { id: Provider; label: string; model: 'claude_model' | 'codex_model' }[] = [
  { id: 'claude', label: 'Claude', model: 'claude_model' },
  { id: 'codex', label: 'Codex', model: 'codex_model' },
]

const messageOf = (error: unknown) => (error instanceof Error ? error.message : String(error))

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    dialog.current?.showModal()
    let alive = true
    api.getSettings().then(
      (loaded) => alive && setSettings(loaded),
      (error) => alive && setProblem(`Could not load the settings. ${messageOf(error)}`),
    )
    return () => {
      alive = false
    }
  }, [])

  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!settings) return
    setSaving(true)
    setProblem(null)
    try {
      await api.updateSettings(settings)
      onClose()
    } catch (error) {
      setProblem(`Could not save the settings. ${messageOf(error)}`)
      setSaving(false)
    }
  }

  return (
    <dialog className="dialog" ref={dialog} onClose={onClose}>
      <form onSubmit={save}>
        <h2>Settings</h2>
        <p className="dialog-lead">The tagger reads each paragraph and decides which character says each line.</p>

        {settings && (
          <div className="provider-list">
            {PROVIDERS.map((provider) => (
              <div key={provider.id} className="provider" data-chosen={settings.provider === provider.id || undefined}>
                <label className="provider-choice">
                  <input
                    type="radio"
                    name="provider"
                    checked={settings.provider === provider.id}
                    onChange={() => setSettings({ ...settings, provider: provider.id })}
                  />
                  {provider.label}
                </label>
                <label className="field">
                  <span>Model</span>
                  <input
                    type="text"
                    value={settings[provider.model]}
                    spellCheck={false}
                    onChange={(event) => setSettings({ ...settings, [provider.model]: event.target.value })}
                  />
                </label>
              </div>
            ))}
          </div>
        )}
        {settings && (
          <div className="illustration-settings">
            <label className="field">
              <span>Image service URL</span>
              <input
                type="text"
                value={settings.image_url}
                spellCheck={false}
                placeholder="http://192.168.1.20:8000"
                onChange={(event) => setSettings({ ...settings, image_url: event.target.value })}
              />
            </label>
            <label className="provider-choice">
              <input
                type="checkbox"
                checked={settings.illustrations}
                disabled={settings.image_url.trim() === ''}
                onChange={(event) => setSettings({ ...settings, illustrations: event.target.checked })}
              />
              Draw pictures
            </label>
            <label className="field field-wide">
              <span>Pictures a page</span>
              <select
                value={settings.pictures_per_page}
                onChange={(event) => setSettings({ ...settings, pictures_per_page: Number(event.target.value) })}
              >
                {Array.from({ length: 12 }, (_, n) => n + 1).map((count) => (
                  <option key={count}>{count}</option>
                ))}
              </select>
            </label>
            <p className="field-help">
              Every moment worth a picture is found, and on each page the most important ones are drawn, up to this
              number. A picture takes about 20 seconds.
            </p>
            <p className="field-help">
              Enables illustrations of key moments while you read. Enter the address of the image service from
              imager/, or leave it empty for none.
            </p>
          </div>
        )}
        {!settings && !problem && <p>Loading…</p>}
        {problem && (
          <p className="dialog-problem" role="alert">
            {problem}
          </p>
        )}

        <div className="row-actions">
          <button type="button" className="quiet-button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button" disabled={!settings || saving}>
            Save settings
          </button>
        </div>
      </form>
    </dialog>
  )
}
