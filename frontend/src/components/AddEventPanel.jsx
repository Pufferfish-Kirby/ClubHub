import { Check, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { confirmEvent } from '@/lib/api'
import { motion, sky } from '@/lib/theme'
import { cn } from '@/lib/utils'

/**
 * One event extraction couldn't fully read: title and location came through, but `event.missing` (see
 * backend/app/services/extraction.py's missing_fields) says which single piece didn't. Asks for exactly
 * that piece -- a full date+time when nothing was legible, or just a time when the date was -- and
 * publishes it immediately (POST /events/:id/confirm) rather than it sitting in pending_review forever.
 * @param {{ event: object, onConfirmed: (eventId: string, updated: object) => void }} props
 */
function ConfirmEventForm({ event, onConfirmed }) {
  const needsDate = event.missing?.includes('date') ?? true
  // start_local is a bare date ("2026-09-25") in exactly the "missing time" case (see missing_fields):
  // the model read a date but no time of day.
  const knownDate = !needsDate ? event.start_local : null
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!value || busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await confirmEvent(event.id, needsDate ? value : `${knownDate}T${value}:00`, event.confirm_token)
      onConfirmed(event.id, updated)
    } catch (err) {
      setError(typeof err?.response?.data?.detail === 'string' ? err.response.data.detail : "Couldn't save that.")
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <p className="truncate text-sm" title={event.title}>
        {event.title}
      </p>
      <div className="flex items-center gap-2">
        {needsDate ? (
          <Input
            type="datetime-local"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label={`Date and time for ${event.title}`}
            required
            className="h-8 flex-1"
          />
        ) : (
          <>
            <span className="shrink-0 text-xs text-muted-foreground">
              {new Date(`${knownDate}T00:00:00`).toLocaleDateString(undefined, {
                month: 'short',
                day: 'numeric',
              })}
            </span>
            <Input
              type="time"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              aria-label={`Time for ${event.title}`}
              required
              className="h-8 flex-1"
            />
          </>
        )}
        <Button type="submit" size="sm" disabled={!value || busy}>
          {busy ? 'Saving…' : 'Confirm'}
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-foreground/90">
          {error}
        </p>
      )}
    </form>
  )
}

/**
 * What happened to the poster the student just chose: reading it, the server's answer, or a plain error. It is
 * feedback only: the "Drop a poster" button in the header opens the file picker, and this hangs under it. It never
 * promises an event will appear; the wording of a result comes from the server.
 * A result the extraction couldn't fully read comes back as a `confirm` phase: one small form per event asking for
 * just the missing date or time (POST /events/:id/confirm), which publishes it.
 * @param {{ state: { phase: 'idle' | 'uploading' | 'confirm' | 'done' | 'error', name?: string, ok?: boolean,
 *   message?: string, pending?: object[] },
 *   onClose: () => void, onAgain: () => void, onConfirmed: (eventId: string, updated: object) => void,
 *   className?: string }} props
 */
export default function AddEventPanel({ state, onClose, onAgain, onConfirmed, className }) {
  const { phase } = state
  useEffect(() => {
    if (phase === 'idle' || phase === 'uploading') return undefined
    const onKey = (e) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [phase, onClose])

  if (phase === 'idle') return null

  return (
    <div
      role={phase === 'error' ? 'alert' : 'status'}
      aria-live="polite"
      style={{ animationDuration: `${motion.base}ms` }}
      className={cn(
        'absolute top-full right-0 z-30 mt-2 flex w-[min(21rem,calc(100vw-3rem))] flex-col gap-3 rounded-xl border border-border bg-card p-4',
        'shadow-[0_16px_48px_rgba(0,0,0,0.5)] animate-in fade-in slide-in-from-top-1 motion-reduce:animate-none',
        className,
      )}
    >
      {phase === 'uploading' ? (
        <div className="flex flex-col items-center gap-3 py-3 text-center">
          <div aria-hidden className="flex gap-2">
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="twinkle size-1.5 rounded-full"
                style={{ background: sky.stars, animationDelay: `${i * 0.2}s` }}
              />
            ))}
          </div>
          <p className="text-sm">Reading your poster…</p>
          <p className="max-w-full truncate text-xs text-muted-foreground">{state.name} · about 10 seconds</p>
        </div>
      ) : phase === 'confirm' ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-start justify-between gap-3">
            <p className="text-sm leading-snug text-muted-foreground">
              {state.message}{' '}
              {state.pending.length === 1
                ? "We couldn't read this one clearly. Mind confirming it?"
                : `We couldn't read ${state.pending.length} of these clearly. Mind confirming them?`}
            </p>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="rounded-sm p-1 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X aria-hidden className="size-4" />
            </button>
          </div>
          <div className="flex flex-col gap-2.5">
            {state.pending.map((event) => (
              <ConfirmEventForm key={event.id} event={event} onConfirmed={onConfirmed} />
            ))}
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-start justify-between gap-3">
            <p className="flex gap-2 text-sm leading-snug">
              {phase === 'done' && state.ok && <Check aria-hidden className="mt-0.5 size-4 shrink-0 text-foreground" />}
              <span>{state.message}</span>
            </p>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="rounded-sm p-1 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X aria-hidden className="size-4" />
            </button>
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={onAgain}>
              {phase === 'error' ? 'Try again' : 'Add another'}
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              {phase === 'error' ? 'Dismiss' : 'Done'}
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
