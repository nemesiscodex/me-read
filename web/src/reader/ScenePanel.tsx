import { useEffect, useState } from 'react'
import { api, type Moment } from '../api'

interface Props {
  /** The picture to show, or null for an empty frame. */
  shown: Moment | null
  /** The moment the reading has reached. It differs from `shown` while its picture is not drawn. */
  reached: Moment | null
  onMoment: (moment: Moment) => void
}

function SceneImage({ url, alt, onShown }: { url: string; alt: string; onShown: () => void }) {
  const [loaded, setLoaded] = useState(false)
  return <img src={url} alt={alt} data-loaded={loaded || undefined} onLoad={() => setLoaded(true)} onTransitionEnd={onShown} />
}

/** The frame above the text. It keeps its size whatever it holds, so a picture never moves the text. */
export function ScenePanel({ shown, reached, onMoment }: Props) {
  const url = shown ? api.momentUrl(shown) : null
  // The picture that has finished fading in stays underneath while the next one fades in over it.
  const [under, setUnder] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (url === null) setUnder(null)
  }, [url])

  const failed = reached !== null && reached.kind === 'picture' && reached.image_state === 'failed' ? reached : null
  const target = failed ?? shown
  const redraw = () => {
    if (!target) return
    setBusy(true)
    api.redrawMoment(target.id).then(onMoment, () => {}).finally(() => setBusy(false))
  }

  return (
    <figure className="scene">
      <div className="scene-frame" style={url && under ? { backgroundImage: `url("${under}")` } : undefined}>
        {shown && url && <SceneImage key={url} url={url} alt={shown.scene} onShown={() => setUnder(url)} />}
        {target && (
          <button
            className="scene-redraw"
            data-failed={failed !== null || undefined}
            disabled={busy}
            onClick={redraw}
            title={failed ? (failed.error ?? undefined) : 'Draw this moment again'}
          >
            {failed ? 'This moment could not be drawn. Try again' : 'Draw again'}
          </button>
        )}
      </div>
    </figure>
  )
}
