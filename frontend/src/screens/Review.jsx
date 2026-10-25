import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { discardReviewed, getPendingReview, publishReviewed } from '@/lib/api'

// The key lives for this tab only: a reload doesn't ask again, closing the tab forgets it.
const KEY_STORAGE = 'clubhub:review-key'
function loadKey() {
  try {
    return sessionStorage.getItem(KEY_STORAGE) ?? ''
  } catch {
    return ''
  }
}
function saveKey(key) {
  try {
    if (key) sessionStorage.setItem(KEY_STORAGE, key)
    else sessionStorage.removeItem(KEY_STORAGE)
  } catch {
    // storage unavailable: the key just isn't remembered
  }
}

const detail = (err) => (typeof err?.response?.data?.detail === 'string' ? err.response.data.detail : "Couldn't save that.")

/** One pending event: check the poster, type the start, publish it or discard it. */
function PendingRow({ event, reviewKey, onDone, onWrongKey }) {
  // A poster with a date but no time is stored at local midnight. Never pre-fill that as a time: one tap on Publish
  // would put a midnight event on the feed. Make the reviewer type the time instead. (Ingest knows the difference,
  // extraction.has_time_component, but Event doesn't store it, so 00:00 is the only signal left here.)
  const [knownDate, knownTime] = event.start_local?.split('T') ?? ['', '']
  const [date, setDate] = useState(knownDate)
  const [time, setTime] = useState(knownTime === '00:00' ? '' : knownTime)
  const start = date && time ? `${date}T${time}` : ''
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function act(fn) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onDone(event.id)
    } catch (err) {
      const status = err?.response?.status
      // The key was changed on the server while this tab was open: back to the key form.
      if (status === 401) return onWrongKey()
      // Another reviewer already published or discarded it. It isn't waiting any more, so drop the row.
      if (status === 409) return onDone(event.id)
      setError(detail(err))
      setBusy(false)
    }
  }

  function discard() {
    if (window.confirm(`Discard "${event.title}"? It won't be shown to students.`)) {
      act(() => discardReviewed(event.id, reviewKey))
    }
  }

  return (
    <li className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex items-baseline justify-between gap-2">
        <p className="font-medium">{event.title}</p>
        {event.dropbox_link && (
          <a href={event.dropbox_link} target="_blank" rel="noreferrer" className="shrink-0 text-xs underline">
            Open poster
          </a>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {event.club_name}
        {event.location && ` · ${event.location}`}
      </p>
      <p className="text-xs text-muted-foreground">
        {!event.start_local
          ? 'No date found. Read it off the poster.'
          : knownTime === '00:00'
            ? 'No time found. Read it off the poster.'
            : 'Check the date and time against the poster.'}
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (start && !busy) act(() => publishReviewed(event.id, start, reviewKey))
        }}
        className="flex flex-col gap-2"
      >
        <div className="grid grid-cols-2 gap-2">
          <Input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            aria-label={`Date for ${event.title}`}
            required
            className="h-8"
          />
          <Input
            type="time"
            value={time}
            onChange={(e) => setTime(e.target.value)}
            aria-label={`Time for ${event.title}`}
            required
            className="h-8"
          />
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={discard}>
            Discard
          </Button>
          <Button type="submit" size="sm" disabled={!start || busy}>
            Publish
          </Button>
        </div>
      </form>
      {error && (
        <p role="alert" className="text-xs text-foreground/90">
          {error}
        </p>
      )}
    </li>
  )
}

/** ?view=review: pending Dropbox events nobody was there to confirm (backend/app/routers/review.py). */
export default function Review() {
  const [key, setKey] = useState(loadKey)
  const [draft, setDraft] = useState('')
  // Tagged with the key it was fetched for, so a new key reads as loading without resetting state in the effect.
  const [result, setResult] = useState({ key: null, events: null, error: null })

  useEffect(() => {
    if (!key) return
    getPendingReview(key)
      .then((events) => setResult({ key, events, error: null }))
      .catch((err) => {
        const status = err?.response?.status
        if (status === 401) {
          saveKey('')
          setKey('')
        }
        const error =
          status === 401
            ? 'That key is wrong.'
            : status === 404
              ? "Review is turned off on this server (REVIEW_KEY isn't set)."
              : "Couldn't load the queue."
        setResult({ key, events: null, error })
      })
  }, [key])

  function signIn(e) {
    e.preventDefault()
    saveKey(draft)
    setKey(draft)
    setDraft('')
  }

  function signOut() {
    saveKey('')
    setKey('')
    setResult({ key: null, events: null, error: 'That key is wrong.' })
  }

  const loading = Boolean(key) && result.key !== key
  const events = result.key === key ? result.events : null
  const error = loading ? null : result.error
  const setEvents = (update) => setResult((r) => ({ ...r, events: update(r.events) }))

  return (
    <div className="mx-auto flex min-h-svh max-w-2xl flex-col gap-4 bg-background px-4 py-10 text-foreground">
      <h1 className="text-xl font-semibold">Posters to review</h1>
      {!key ? (
        <form onSubmit={signIn} className="flex gap-2">
          <Input
            type="password"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Review key"
            placeholder="Review key"
            className="h-9 flex-1"
          />
          <Button type="submit" disabled={!draft}>
            Open
          </Button>
        </form>
      ) : loading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : events?.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing waiting. Every poster has been checked.</p>
      ) : (
        events && (
          <ul className="flex flex-col gap-3">
            {events.map((ev) => (
              <PendingRow
                key={ev.id}
                event={ev}
                reviewKey={key}
                onDone={(id) => setEvents((list) => list.filter((x) => x.id !== id))}
                onWrongKey={signOut}
              />
            ))}
          </ul>
        )
      )}
      {error && (
        <p role="alert" className="text-sm text-foreground/90">
          {error}
        </p>
      )}
    </div>
  )
}
