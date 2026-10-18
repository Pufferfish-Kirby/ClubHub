import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Info, Mail, X } from 'lucide-react'
import DropboxProvenance from '@/components/DropboxProvenance'
import OutcomePill from '@/components/OutcomePill'
import PosterInvite from '@/components/PosterInvite'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { isLimitedSummary, scrubText, toEventLite } from '@/lib/club'
import { formatEventTime } from '@/lib/format'
import { motion } from '@/lib/theme'
import { cn } from '@/lib/utils'
import { normalizeOutcome } from '@/types'

/** Width of the panel over the graph on large screens. StarGraph refits the stars into the space left of it. */
export const PANEL_WIDTH = 340
export const PANEL_GAP = 12

const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1)

function Section({ title, children }) {
  return (
    <section className="flex flex-col gap-2">
      <h4 className="text-[0.7rem] font-medium tracking-[0.08em] text-muted-foreground uppercase">{title}</h4>
      {children}
    </section>
  )
}

function ExternalLink({ href, children, className, kind = 'link' }) {
  const isEmail = kind === 'email' || href.startsWith('mailto:')
  return (
    <a
      href={href}
      {...(isEmail ? {} : { target: '_blank', rel: 'noopener noreferrer' })}
      className={cn(
        'inline-flex items-center gap-0.5 rounded-sm text-sm text-foreground underline-offset-2 outline-none',
        'hover:underline focus-visible:ring-2 focus-visible:ring-ring',
        className,
      )}
    >
      {children}
      {isEmail ? (
        <Mail aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
      ) : (
        <ArrowUpRight aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
      )}
    </a>
  )
}

/** Clicking the title/time opens this event in the panel's own "Event" tab; the provenance/RSVP row stays separate
 * so its links keep their own click targets. */
function EventItem({ event, onOpen }) {
  const line = [formatEventTime(event.start), event.location].filter(Boolean).join(' · ')
  return (
    <li className="border-t border-border pt-2.5 first:border-t-0 first:pt-0">
      <button
        type="button"
        onClick={onOpen}
        className="flex w-full flex-col gap-1 rounded-sm text-left underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
      >
        <p className="text-sm leading-snug">{event.title}</p>
        {line && <p className="text-xs text-muted-foreground">{line}</p>}
      </button>
      {(event.source === 'dropbox' || event.rsvp_url) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1.5">
          <DropboxProvenance event={event} />
          {event.rsvp_url && (
            <ExternalLink href={event.rsvp_url} className="text-xs">
              RSVP
            </ExternalLink>
          )}
        </div>
      )}
    </li>
  )
}

function TabButton({ active, onClick, children }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'rounded-md px-2.5 py-1 text-xs font-medium outline-none transition-colors',
        'focus-visible:ring-2 focus-visible:ring-ring',
        active ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground',
      )}
    >
      {children}
    </button>
  )
}

/** The panel's "Event" tab: what a specific event star (or arrival toast) explains, in full -- the one place
 * Event.description (scrubbed, lib/club.js) is shown, since the compact Upcoming list never had room for it. */
function EventDetail({ event }) {
  const line = [formatEventTime(event.start), event.location].filter(Boolean).join(' · ')
  return (
    <>
      <Section title="When & where">
        <p className="text-sm text-foreground/90">{line || 'Time not confirmed yet.'}</p>
      </Section>
      <Section title="About this event">
        {event.description ? (
          <p className="text-sm leading-relaxed text-foreground/90">{event.description}</p>
        ) : (
          <p className="flex items-start gap-1.5 text-sm text-muted-foreground">
            <Info aria-hidden className="mt-0.5 size-3.5 shrink-0" />
            No description available.
          </p>
        )}
      </Section>
      {(event.source === 'dropbox' || event.rsvp_url) && (
        <Section title="Source">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <DropboxProvenance event={event} />
            {event.rsvp_url && (
              <ExternalLink href={event.rsvp_url} className="text-sm">
                RSVP
              </ExternalLink>
            )}
          </div>
        </Section>
      )}
    </>
  )
}

/**
 * Club detail over the right side of the graph (a bottom sheet on small screens).
 * Renders at once from the ranked match (why it fits, next event); GET /clubs/:id fills in the rest.
 * Never shows organizer contacts: `detail` comes from sanitizeClub in lib/club.js.
 * @param {{ club: object, rank?: number, detail: import('@/lib/club').ClubDetail | null, loading: boolean,
 *   error: Error | null, notFound?: boolean, selectedEventId?: string | null, onSelectEvent?: (id: string) => void,
 *   onRetry: () => void, onClose: () => void,
 *   mapAction?: { label: string, onClick: () => void, variant?: string, disabled?: boolean, icon?: import('react').ReactNode } | null }} props
 * `mapAction`: the Directory's "Add to your map" / the constellation's "Remove from your map" (lib/myClubs.js) --
 * left to the caller since what it does (and whether it's even offered) differs by screen.
 */
export default function ClubPanel({
  club,
  rank,
  detail,
  loading,
  error,
  notFound = false,
  selectedEventId = null,
  onSelectEvent,
  onRetry,
  onClose,
  mapAction = null,
}) {
  const scrollRef = useRef(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0 })
  }, [club.id])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const keys = [...new Set((club.outcomes ?? []).map(normalizeOutcome).filter(Boolean))]
  const commitment = detail?.commitment ?? club.commitment
  const commitmentText = commitment && commitment !== 'unknown' ? `${capitalize(commitment)} commitment` : null

  const why = scrubText(club.why_it_fits)
  const summary = detail ? detail.summary : scrubText(club.summary) || null
  // Before details arrive (or if they can't), fall back to the match's next event.
  const events = detail ? detail.events : club.next_event ? [toEventLite(club.next_event)] : []
  const links = detail?.links ?? []

  // The event the "Event" tab explains: whichever one a graph star or arrival toast pointed at, else the soonest
  // upcoming (so the tab still means something if it's opened by hand). Hidden entirely when there's no event at all.
  const eventForTab = events.find((e) => e.id === selectedEventId) ?? events[0] ?? null
  const [tab, setTab] = useState(selectedEventId ? 'event' : 'club')
  // Reset the tab when a different club or event is picked. Done during render rather than in an
  // effect so the old tab never paints (react.dev: "adjusting some state when a prop changes").
  const [tabFor, setTabFor] = useState({ clubId: club.id, eventId: selectedEventId })
  if (tabFor.clubId !== club.id || tabFor.eventId !== selectedEventId) {
    setTabFor({ clubId: club.id, eventId: selectedEventId })
    setTab(selectedEventId ? 'event' : 'club')
  }

  return (
    <aside
      aria-label={`${club.name} details`}
      style={{ '--panel-w': `${PANEL_WIDTH}px`, '--panel-gap': `${PANEL_GAP}px`, animationDuration: `${motion.base}ms` }}
      className={cn(
        'z-20 flex flex-col overflow-hidden rounded-xl border border-border bg-card shadow-[0_16px_48px_rgba(0,0,0,0.5)]',
        'fixed inset-x-3 bottom-3 max-h-[75svh]',
        'lg:absolute lg:inset-x-auto lg:top-(--panel-gap) lg:right-(--panel-gap) lg:bottom-(--panel-gap) lg:max-h-none lg:w-(--panel-w)',
        'animate-in fade-in slide-in-from-right-2 motion-reduce:animate-none',
      )}
    >
      <header className="flex items-start gap-3 border-b border-border px-5 pt-4 pb-3.5">
        {rank != null && (
          <span className="shrink-0 pt-[7px] text-xs text-muted-foreground tabular-nums">{String(rank).padStart(2, '0')}</span>
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h3 className="text-xl leading-tight">{club.name}</h3>
            {club._addedByUser && <Badge variant="secondary">Added by you</Badge>}
          </div>
          {keys.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {keys.map((k) => (
                <OutcomePill key={k} outcomeKey={k} />
              ))}
            </div>
          )}
          {commitmentText && <p className="text-xs text-muted-foreground">{commitmentText}</p>}
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          className="-mr-1.5 shrink-0 text-muted-foreground hover:text-foreground"
          aria-label="Close details"
          onClick={onClose}
        >
          <X />
        </Button>
      </header>

      {eventForTab && (
        <div className="flex items-center gap-1 border-b border-border px-4 py-2">
          <TabButton active={tab === 'club'} onClick={() => setTab('club')}>
            Club
          </TabButton>
          <TabButton active={tab === 'event'} onClick={() => setTab('event')}>
            Event
          </TabButton>
        </div>
      )}

      {mapAction && (
        <div className="border-b border-border px-5 py-2.5">
          <Button
            size="sm"
            variant={mapAction.variant ?? 'outline'}
            disabled={mapAction.disabled}
            onClick={mapAction.onClick}
            className="w-full justify-center"
          >
            {mapAction.icon}
            {mapAction.label}
          </Button>
        </div>
      )}

      <div ref={scrollRef} className="flex flex-col gap-5 overflow-y-auto px-5 pt-4 pb-5">
        {tab === 'event' && eventForTab ? (
          <EventDetail event={eventForTab} />
        ) : (
          <>
            {why && (
              <Section title="Why it fits you">
                <p className="text-sm leading-relaxed text-foreground/90">{why}</p>
              </Section>
            )}

            <Section title="About">
              {isLimitedSummary(summary) ? (
                <p className="flex items-start gap-1.5 text-sm text-muted-foreground">
                  <Info aria-hidden className="mt-0.5 size-3.5 shrink-0" />
                  Limited info so far. The SOP listing may have more.
                </p>
              ) : (
                <p className="text-sm leading-relaxed text-foreground/90">{summary}</p>
              )}
            </Section>

            {detail?.meeting_info && (
              <Section title="Meets">
                <p className="text-sm text-foreground/90">{detail.meeting_info}</p>
              </Section>
            )}

            <Section title="Upcoming">
              {events.length > 0 ? (
                <ul className="flex flex-col gap-2.5">
                  {events.map((ev) => (
                    <EventItem key={ev.id} event={ev} onOpen={() => onSelectEvent?.(ev.id)} />
                  ))}
                </ul>
              ) : loading ? (
                <div aria-hidden className="flex flex-col gap-2">
                  <Skeleton className="h-4 w-3/4" />
                  <Skeleton className="h-3 w-1/2" />
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No upcoming events yet
                  <PosterInvite />
                </p>
              )}
            </Section>

            {loading && !detail ? (
              <div aria-hidden className="flex flex-col gap-2">
                <Skeleton className="h-3 w-16" />
                <Skeleton className="h-4 w-28" />
              </div>
            ) : (
              links.length > 0 && (
                <Section title="Links">
                  <ul className="flex flex-col gap-1.5">
                    {links.map((l) => (
                      <li key={l.href}>
                        <ExternalLink href={l.href} kind={l.kind}>{l.label}</ExternalLink>
                      </li>
                    ))}
                  </ul>
                </Section>
              )
            )}

            {error && (
              <p className="text-xs text-muted-foreground">
                Couldn't load the full listing.{' '}
                <button
                  type="button"
                  onClick={onRetry}
                  className="rounded-sm text-foreground underline underline-offset-2 outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Try again
                </button>
              </p>
            )}
            {import.meta.env.DEV && notFound && (
              <p className="text-xs text-muted-foreground">Dev: GET /clubs/{club.id} returned 404.</p>
            )}
          </>
        )}
      </div>
    </aside>
  )
}
