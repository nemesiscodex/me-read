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
        <h2>Tagger settings</h2>
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
