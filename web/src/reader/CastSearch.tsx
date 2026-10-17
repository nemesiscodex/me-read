interface Props {
  value: string
  onChange: (value: string) => void
  /** Enter picks the first character shown. */
  onPick: () => void
}

/** Focused straight away with a mouse; on touch screens that would throw up the keyboard. */
const finePointer = () => window.matchMedia('(pointer: fine)').matches

export function CastSearch({ value, onChange, onPick }: Props) {
  return (
    <input
      type="search"
      className="cast-search"
      placeholder="Find a character"
      aria-label="Find a character"
      value={value}
      autoFocus={finePointer()}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          onPick()
        } else if (event.key === 'Escape' && value !== '') {
          // Clears the search instead of closing the panel around it.
          event.stopPropagation()
          onChange('')
        }
      }}
    />
  )
}
