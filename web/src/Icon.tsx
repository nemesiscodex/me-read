const PATHS = {
  play: 'M7 4.5v15l12-7.5z',
  pause: 'M7 5h3.5v14H7zM13.5 5H17v14h-3.5z',
  previous: 'M6 5h2.5v14H6zM19 5v14l-9.5-7z',
  next: 'M15.5 5H18v14h-2.5zM5 5v14l9.5-7z',
  left: 'M14.5 5.5 8 12l6.5 6.5',
  right: 'M9.5 5.5 16 12l-6.5 6.5',
  more: 'M5 10.5h3v3H5zM10.5 10.5h3v3h-3zM16 10.5h3v3h-3z',
  close: 'M6 6l12 12M18 6 6 18',
} as const

const STROKED: ReadonlySet<IconName> = new Set(['left', 'right', 'close'])

export type IconName = keyof typeof PATHS

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const stroked = STROKED.has(name)
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      fill={stroked ? 'none' : 'currentColor'}
      stroke={stroked ? 'currentColor' : 'none'}
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
