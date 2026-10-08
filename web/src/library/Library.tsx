import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { api, type BookSummary } from '../api'
import { bookHref } from '../lib/route'
import { SettingsDialog } from './SettingsDialog'
import './library.css'

const messageOf = (error: unknown) => (error instanceof Error ? error.message : String(error))

const isEpub = (file: File) => file.name.toLowerCase().endsWith('.epub')

function progress(book: BookSummary): number {
  if (book.block_count <= 1) return 0
  return Math.min(1, Math.max(0, book.position_block / (book.block_count - 1)))
}

function hasFiles(event: DragEvent): boolean {
  return event.dataTransfer.types.includes('Files')
}

export function Library() {
  const [books, setBooks] = useState<BookSummary[] | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [dropping, setDropping] = useState(false)
  const [deleting, setDeleting] = useState<number | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const dragDepth = useRef(0)

  useEffect(() => {
    let alive = true
    api.listBooks().then(
      (list) => alive && setBooks(list),
      (error) => alive && setProblem(`Could not load your books. ${messageOf(error)}`),
    )
    return () => {
      alive = false
    }
  }, [])

  const addBooks = useCallback(async (files: File[]) => {
    const epubs = files.filter(isEpub)
    if (epubs.length === 0) return setProblem('Only .epub files can be added.')
    setProblem(null)
    for (const file of epubs) {
      setAdding(file.name)
      try {
        const book = await api.uploadBook(file)
        setBooks((list) => [book, ...(list ?? []).filter((b) => b.id !== book.id)])
      } catch (error) {
        setProblem(`Could not add ${file.name}. ${messageOf(error)}`)
      }
    }
    setAdding(null)
  }, [])

  const deleteBook = async (book: BookSummary) => {
    try {
      await api.deleteBook(book.id)
      setBooks((list) => (list ?? []).filter((b) => b.id !== book.id))
    } catch (error) {
      setProblem(`Could not delete ${book.title}. ${messageOf(error)}`)
    }
    setDeleting(null)
  }

  const onDragEnter = (event: DragEvent) => {
    if (!hasFiles(event)) return
    dragDepth.current += 1
    setDropping(true)
  }
  const onDragLeave = (event: DragEvent) => {
    if (!hasFiles(event)) return
    dragDepth.current -= 1
    if (dragDepth.current <= 0) setDropping(false)
  }
  const onDrop = (event: DragEvent) => {
    if (!hasFiles(event)) return
    event.preventDefault()
    dragDepth.current = 0
    setDropping(false)
    void addBooks([...event.dataTransfer.files])
  }

  return (
    <div
      className="library"
      data-dropping={dropping || undefined}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={(event) => hasFiles(event) && event.preventDefault()}
      onDrop={onDrop}
    >
      <header className="library-top">
        <h1 className="wordmark">me-read</h1>
        <div className="library-actions">
          <button className="quiet-button" onClick={() => setSettingsOpen(true)}>
            Tagger settings
          </button>
          <button className="button" onClick={() => fileInput.current?.click()} disabled={adding !== null}>
            Add an EPUB
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".epub,application/epub+zip"
            multiple
            hidden
            onChange={(event) => {
              const files = [...(event.target.files ?? [])]
              event.target.value = ''
              if (files.length > 0) void addBooks(files)
            }}
          />
        </div>
      </header>

      {problem && (
        <p className="library-problem" role="alert">
          {problem}
        </p>
      )}
      {adding && (
        <p className="library-adding" role="status">
          Adding {adding}…
        </p>
      )}

      {books === null && !problem && <p className="library-empty">Loading your books…</p>}

      {books !== null && books.length === 0 && (
        <div className="library-empty">
          <p className="library-empty-lead">Nothing on the shelf yet.</p>
          <p>Drop an EPUB anywhere on this page, or use Add an EPUB. It will be read to you, one voice per character.</p>
        </div>
      )}

      {books !== null && books.length > 0 && (
        <ul className="shelf">
          {books.map((book) => (
            <li key={book.id} className="shelf-item">
              <a className="shelf-book" href={bookHref(book.id)}>
                <span className="shelf-title">{book.title}</span>
                <span className="shelf-author">{book.author}</span>
                <span className="shelf-progress" aria-label={`${Math.round(progress(book) * 100)}% read`}>
                  <span style={{ width: `${progress(book) * 100}%` }} />
                </span>
              </a>
              {deleting === book.id ? (
                <span className="shelf-confirm">
                  Delete this book and its audio?
                  <button className="button danger" onClick={() => void deleteBook(book)}>
                    Delete
                  </button>
                  <button className="quiet-button" onClick={() => setDeleting(null)}>
                    Keep
                  </button>
                </span>
              ) : (
                <button className="quiet-button shelf-delete" onClick={() => setDeleting(book.id)}>
                  Delete
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {dropping && <div className="drop-veil">Drop to add to your library</div>}
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
    </div>
  )
}
