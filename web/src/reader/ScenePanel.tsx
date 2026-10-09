import { useEffect, useRef, useState } from 'react'
import { api, type Moment } from '../api'

interface Caption {
  text: string
  /** Null for narration. */
  speaker: string | null
}

interface Props {
  /** The picture to show, or null for an empty frame. */
  shown: Moment | null
  /** The moment the reading has reached. It differs from `shown` while its picture is not drawn. */
  reached: Moment | null
  /** The line being read, shown under the picture while it fills the screen. */
  caption: Caption | null
  onMoment: (moment: Moment) => void
}

/** The picture in words, for someone who cannot see it. */
function describe(moment: Moment): string {
  return moment.summary || moment.scene
}

function SceneImage({ url, alt, onShown, onZoom }: { url: string; alt: string; onShown: () => void; onZoom: () => void }) {
  const [loaded, setLoaded] = useState(false)
  return (
    <img
      src={url}
      alt={alt}
      data-loaded={loaded || undefined}
      onLoad={() => setLoaded(true)}
      onTransitionEnd={onShown}
      onClick={onZoom}
    />
  )
}

/** The picture over the whole screen. It follows the reading, so it changes when the next moment is reached. */
function SceneZoom({ url, moment, caption, onClose }: { url: string; moment: Moment; caption: Caption | null; onClose: () => void }) {
  const overlay = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = overlay.current
    // Where the browser refuses full screen the picture still fills the window.
    el?.requestFullscreen?.().catch(() => {})
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    const onChange = () => {
      if (document.fullscreenElement === null) onClose()
    }
    window.addEventListener('keydown', onKey)
    document.addEventListener('fullscreenchange', onChange)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.removeEventListener('fullscreenchange', onChange)
      if (document.fullscreenElement === el) void document.exitFullscreen()
    }
  }, [onClose])

  return (
    <div className="scene-zoom" ref={overlay} role="dialog" aria-label="Picture, full screen" onClick={onClose}>
      <img src={url} alt={describe(moment)} />
      {moment.title && <p className="scene-zoom-title">{moment.title}</p>}
      {caption && (
        <p className="scene-caption" aria-live="off">
          {caption.speaker && <span className="scene-caption-speaker">{caption.speaker}</span>}
          {caption.text}
        </p>
      )}
      <button className="scene-zoom-close" onClick={onClose}>
        Close
      </button>
    </div>
  )
}

/** The frame above the text. It keeps its size whatever it holds, so a picture never moves the text. */
export function ScenePanel({ shown, reached, caption, onMoment }: Props) {
  const url = shown ? api.momentUrl(shown) : null
  // The picture that has finished fading in stays underneath while the next one fades in over it.
  const [under, setUnder] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [zoomed, setZoomed] = useState(false)
  const closeZoom = useRef(() => setZoomed(false)).current

  useEffect(() => {
    if (url === null) {
      setUnder(null)
      setZoomed(false)
    }
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
        {shown && url && (
          <SceneImage key={url} url={url} alt={describe(shown)} onShown={() => setUnder(url)} onZoom={() => setZoomed(true)} />
        )}
        {shown?.title && (
          <p className="scene-title" title={shown.summary}>
            {shown.title}
          </p>
        )}
        {shown && url && (
          <button className="scene-enlarge" onClick={() => setZoomed(true)}>
            Full screen
          </button>
        )}
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
      {zoomed && shown && url && <SceneZoom url={url} moment={shown} caption={caption} onClose={closeZoom} />}
    </figure>
  )
}
