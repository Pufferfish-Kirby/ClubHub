import axios from 'axios'

export const api = axios.create({
  baseURL: '/api',
})

/**
 * @param {string} blurb
 * @returns {Promise<{outcomes: string[], clubs: Array<{id: string, name: string, summary: string, outcomes: string[], tags: string[], why_it_fits: string}>}>}
 */
export async function recommend(blurb) {
  const { data } = await api.post('/recommend', { blurb })
  return data
}

/**
 * GET /clubs/:id -> { club, events, similar }. The body is the raw database rows: `events` includes
 * pending_review ones and `club` has fields that must not be shown (e.g. description_raw).
 * Display it only through sanitizeClub (lib/club.js).
 * @param {string} id
 * @param {{ signal?: AbortSignal }} [opts]
 * @returns {Promise<{ club: object, events: object[], similar: object[] }>}
 */
export async function getClub(id, { signal } = {}) {
  const { data } = await api.get(`/clubs/${encodeURIComponent(id)}`, { signal })
  return data
}

/**
 * GET /clubs -> every club row (raw database rows, unranked). The Full Directory's browse list is built from
 * this; display through toDirectoryClub (lib/directory.js), which whitelists fields the same way sanitizeClub
 * does for the detail panel.
 * @param {{ signal?: AbortSignal }} [opts]
 * @returns {Promise<object[]>}
 */
export async function getClubs({ signal } = {}) {
  const { data } = await api.get('/clubs', { signal })
  return data
}

/**
 * GET /events?since= -> published events starting at or after `since` (raw database rows).
 * The backend compares `since` with naive UTC columns, so it is sent as naive UTC ("2026-09-19T23:40:00").
 * Display only through toLiveEvents (lib/events.js).
 * @param {{ since?: Date, signal?: AbortSignal }} [opts]
 * @returns {Promise<object[]>}
 */
export async function getEvents({ since, signal } = {}) {
  const params = since ? { since: since.toISOString().slice(0, 19) } : undefined
  const { data } = await api.get('/events', { params, signal })
  return data
}

/**
 * POST /upload: stores the file in the team Dropbox, has Claude read it, and returns what it found
 * (takes about 5-10 seconds). Events in the response are raw rows: display only through lib/events.js.
 * @param {File} file
 * @returns {Promise<{ status: 'processed' | 'duplicate' | 'unsupported' | 'no_events' | 'failed', message: string,
 *   club: { id: string, name: string, created: boolean } | null,
 *   events: { id: string, title: string, start: string | null, location: string | null, status: string,
 *     source_file: string, dropbox_link: string | null }[] }>}
 */
export async function uploadFlier(file) {
  const body = new FormData()
  body.append('file', file)
  const { data } = await api.post('/upload', body)
  return data
}

/**
 * POST /events/:id/confirm -- a human supplies the field extraction couldn't confidently read (see
 * `missing` on an upload response event), publishing it immediately instead of it sitting in
 * pending_review forever. `start` is local Toronto wall-clock time, ISO 8601 with no UTC offset --
 * either a <input type="datetime-local">'s value as-is ("2026-09-24T19:00", no seconds) or a
 * known-date + <input type="time"> combined by the caller ("2026-09-24T19:00:00"). The backend's
 * to_utc() (datetime.fromisoformat) accepts both.
 * @param {string} eventId
 * @param {string} start
 * @param {string} token the event's `confirm_token` from the upload response
 * @returns {Promise<object>} the updated (now published) public event
 */
export async function confirmEvent(eventId, start, token) {
  const { data } = await api.post(`/events/${encodeURIComponent(eventId)}/confirm`, { start, token })
  return data
}
