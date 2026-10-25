import { lazy, Suspense, useEffect, useState } from 'react'
import { buildBlurb } from '@/lib/blurb'
import { motion } from '@/lib/theme'
import { useRecommend } from '@/lib/useRecommend'
import Blurb from '@/screens/Blurb'
import Campus from '@/screens/Campus'
import Major from '@/screens/Major'
import Welcome from '@/screens/Welcome'

// One particle background behind Welcome, Major and Blurb. It lives here, above the step wrapper, so it keeps running
// (no restart or flash) as the steps change. Loaded only when needed.
const LandingBackground = lazy(() => import('@/components/LandingBackground'))

// The two graph screens are the only users of @xyflow/react and d3, so they load on demand and the first screens
// paint without them. MainFlow starts the Constellation download early (see below) so results never wait on it.
const loadConstellation = () => import('@/screens/Constellation')
const Constellation = lazy(loadConstellation)
const Directory = lazy(() => import('@/screens/Directory'))
// ?view=review: the reviewer's queue for Dropbox posters (screens/Review.jsx). Students never load it.
const Review = lazy(() => import('@/screens/Review'))

// Dev only: with ?previewData, open straight on the results screen. Remove after real data lands.
const PREVIEW_DATA = import.meta.env.DEV && new URLSearchParams(window.location.search).has('previewData')

// The Full Directory opens in its own browser tab (Constellation's "Full Directory" button does
// window.open) rather than a step in this file's flow, so browsing/searching there can never touch this
// tab's own graph. No router: one query param, same pattern as ?previewData above.
const VIEW = new URLSearchParams(window.location.search).get('view')

// The answers (major, blurb) live only in this tab's memory: every visit starts with empty boxes. They used to be
// saved in localStorage and restored, so this drops that old key once (no other site data is touched).
const LEGACY_STORAGE_KEY = 'campus-compass:input'
try {
  localStorage.removeItem(LEGACY_STORAGE_KEY)
} catch {
  // storage unavailable: nothing to clear
}
const EMPTY_INPUT = { major: '', blurb_text: '' }

function MainFlow() {
  const [step, setStep] = useState(PREVIEW_DATA ? 'results' : 'campus') // campus | welcome | major | blurb | results
  const [input, setInput] = useState(EMPTY_INPUT)
  // Reduced motion: no animated background (Welcome keeps its static stars).
  const [animate] = useState(() => !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
  const { data, loading, error, run, retry } = useRecommend()

  // Fetch the results screen while they're still typing. The module cache makes repeat calls free. A failed prefetch
  // is ignored here; lazy() imports again when results render, and that's where a real failure shows.
  useEffect(() => {
    if (step === 'major' || step === 'blurb') loadConstellation().catch(() => {})
  }, [step])

  function launch(text) {
    run(buildBlurb(input.major, text))
    setStep('results')
  }

  return (
    <div className="relative min-h-svh bg-background text-foreground">
      {animate && step !== 'results' && (
        <div aria-hidden className="pointer-events-none fixed inset-0 z-0">
          <Suspense fallback={null}>
            <LandingBackground />
          </Suspense>
        </div>
      )}
      <div
        key={step}
        className="relative z-10 animate-in fade-in"
        style={{ animationDuration: `${motion.base}ms` }}
      >
        {step === 'campus' && <Campus onChoose={() => setStep('welcome')} />}
        {step === 'welcome' && <Welcome onExplore={() => setStep('major')} />}
        {step === 'major' && (
          <Major
            major={input.major}
            onChange={(major) => setInput((i) => ({ ...i, major }))}
            onNext={() => setStep('blurb')}
          />
        )}
        {step === 'blurb' && (
          <Blurb
            text={input.blurb_text}
            onChange={(blurb_text) => setInput((i) => ({ ...i, blurb_text }))}
            onLaunch={launch}
          />
        )}
        {step === 'results' && (
          <Suspense fallback={null}>
            <Constellation
              data={data}
              loading={loading}
              error={error}
              retry={retry}
              onEdit={() => setStep('blurb')}
            />
          </Suspense>
        )}
      </div>
    </div>
  )
}

function App() {
  if (VIEW !== 'directory' && VIEW !== 'review') return <MainFlow />
  return (
    <Suspense fallback={<div className="min-h-svh bg-background" />}>
      {VIEW === 'review' ? <Review /> : <Directory />}
    </Suspense>
  )
}

export default App
