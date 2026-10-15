import { useCallback, useRef, useState } from 'react'
import { uploadFlier } from '@/lib/api'

/** What the native picker offers. Phones list their camera and photo library for image types. */
export const PICKER_ACCEPT = 'image/png,image/jpeg,application/pdf'

// Same allowlist and size cap as backend/app/routers/upload.py, checked here first so a bad file never costs a
// Claude call.
const ALLOWED = ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.pdf']
const MAX_BYTES = 10 * 1024 * 1024

const WRONG_FILE = 'Try a PNG, JPG or PDF under 10 MB.'
const EMPTY_FILE = 'That file looks empty. Try another.'
const UPSTREAM = "Couldn't reach Dropbox right now. Try again in a moment."
const OFFLINE = "Couldn't reach the server. Try again in a moment."

const extOf = (name) => (name.match(/\.[^.]+$/)?.[0] ?? '').toLowerCase()

function problemWith(file) {
  if (!file) return null
  if (file.size === 0) return EMPTY_FILE
  if (!ALLOWED.includes(extOf(file.name)) || file.size > MAX_BYTES) return WRONG_FILE
  return null
}

/** Plain copy for a failed request, by status. Backend `detail` text is not shown. */
function errorMessage(e) {
  switch (e?.response?.status) {
    case 413:
    case 415:
      return WRONG_FILE
    case 400:
      return EMPTY_FILE
    case 502:
      return UPSTREAM
    default:
      return OFFLINE
  }
}

/**
 * The poster upload (POST /upload) and what to say about it.
 * States: idle | uploading | done | error. A published event for one of the student's own result clubs goes to
 * `onPublished` (the card and star light up, as for a Dropbox arrival) and the feedback closes; everything else is
 * reported with the server's message. Nothing here promises an event will appear.
 * @param {{ resultClubIds: Set<string>, onPublished: (events: object[]) => void }} opts
 */
export function useFlierUpload({ resultClubIds, onPublished }) {
  const [state, setState] = useState({ phase: 'idle' })
  // Bookkeeping for the confirm phase, mutated synchronously so two forms confirmed close together can't race
  // (each form's callback would otherwise read a stale `state` from its own render).
  const confirming = useRef(null) // { pending, mine, clubId, publishedCount } | null
  const reset = useCallback(() => {
    confirming.current = null
    setState({ phase: 'idle' })
  }, [])

  const upload = useCallback(
    async (file) => {
      const problem = problemWith(file)
      if (problem) {
        setState({ phase: 'error', message: problem })
        return
      }
      setState({ phase: 'uploading', name: file.name })
      try {
        const res = await uploadFlier(file)
        const clubId = res.club?.id != null ? String(res.club.id) : null
        const events = res.events ?? []
        const published = events.filter((e) => e.status === 'published')
        // Anything else came with `missing` (backend extraction.missing_fields) saying what to ask for: it goes to a
        // confirm form for the uploader instead of sitting in review, and is never shown anywhere else.
        const pending = events.filter((e) => e.status !== 'published')
        const mine = clubId != null && resultClubIds.has(clubId)
        if (mine && published.length) {
          onPublished(published.map((e) => ({ ...e, club_id: clubId, source: 'dropbox' })))
        }
        // Only reached after /upload returned 2xx, which is when the backend has stored the file in Dropbox (it does that
        // before reading the poster), so this is the first moment "Saved to Dropbox" is true.
        if (pending.length) {
          confirming.current = { pending, mine, clubId, publishedCount: published.length }
          setState({ phase: 'confirm', pending, message: 'Saved to Dropbox.' })
          return
        }
        if (mine && published.length) {
          setState({ phase: 'idle' })
          return
        }
        setState({ phase: 'done', ok: published.length > 0 || res.held > 0, message: `Saved to Dropbox · ${res.message}` })
      } catch (e) {
        setState({ phase: 'error', message: errorMessage(e) })
      }
    },
    [resultClubIds, onPublished],
  )

  // One pending event was confirmed (published) by the student.
  const confirmed = useCallback(
    (eventId, updated) => {
      const c = confirming.current
      if (!c) return
      c.pending = c.pending.filter((e) => e.id !== eventId)
      c.publishedCount += 1
      if (c.mine) onPublished([{ ...updated, club_id: c.clubId, source: 'dropbox' }])
      if (c.pending.length) {
        setState({ phase: 'confirm', pending: c.pending, message: 'Saved to Dropbox.' })
        return
      }
      confirming.current = null
      if (c.mine) {
        setState({ phase: 'idle' })
        return
      }
      setState({
        phase: 'done',
        ok: true,
        message: `Saved to Dropbox · Added ${c.publishedCount} event${c.publishedCount === 1 ? '' : 's'}.`,
      })
    },
    [onPublished],
  )

  return { state, upload, reset, confirmed }
}
