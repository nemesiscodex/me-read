import { memo, type CSSProperties } from 'react'
import type { Block } from '../api'
import { characterHue } from '../lib/cast'
import { segmentStatus } from '../lib/segments'

export type ActiveMode = 'playing' | 'paused' | 'waiting'

interface Props {
  block: Block
  narratorId: number | null
  /** Set only on the block that holds the player's segment, so other blocks skip re-rendering. */
  activeId: number | null
  activeMode: ActiveMode | null
}

export const hueStyle = (characterId: number) => ({ '--h': characterHue(characterId) }) as CSSProperties

export const BlockView = memo(function BlockView({ block, narratorId, activeId, activeMode }: Props) {
  const Tag = block.tag === 'h' ? 'h2' : 'p'
  return (
    <Tag className={block.tag === 'h' ? 'block heading' : 'block'} data-block={block.idx}>
      {block.segments.map((segment) => {
        const voiced = segment.character_id !== null && segment.character_id !== narratorId
        return (
          <span
            key={segment.id}
            className="seg"
            data-seg={segment.id}
            data-state={segmentStatus(segment, block.tag_state)}
            data-voiced={voiced || undefined}
            data-active={segment.id === activeId ? (activeMode ?? undefined) : undefined}
            style={voiced ? hueStyle(segment.character_id!) : undefined}
          >
            {segment.text}
          </span>
        )
      })}
    </Tag>
  )
})

export function segmentIdAt(target: EventTarget | null): number | null {
  const el = target instanceof Element ? target.closest<HTMLElement>('[data-seg]') : null
  return el ? Number(el.dataset.seg) : null
}
