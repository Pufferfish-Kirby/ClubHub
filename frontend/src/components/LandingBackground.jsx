import { useEffect, useRef } from 'react'

/**
 * Drifting dot field behind the landing steps. The drawing code is ThreeUI's "Signal Particles" canvas
 * (https://threeui.com, MIT, Copyright (c) 2026 Meng To), lifted out of the package: it shipped that ~50-line canvas
 * inside a 33 kB demo page in an iframe, plus ~420 kB of JS and CSS for effects we never showed.
 * Tuned for the landing page (slower and more saturated than ThreeUI's preset). Change the numbers here.
 */
const SPEED = 0.12 // 1 = the original pace
const SPACING = 16 // px between dots
const DOT_RADIUS = 1.5
// The package applied hue, saturation and brightness as a CSS filter over the whole frame, background included.
const FILTER = 'hue-rotate(-35deg) saturate(0.65) brightness(1.21)'
const BACKGROUND = '#0a0a0a'
const FRAME_MS = 1000 / 60 // the original stepped time once per frame; we step by elapsed time so 120 Hz isn't 2x

function drawFrame(ctx, width, height, time) {
  ctx.clearRect(0, 0, width, height)
  const cols = Math.floor(width / SPACING)
  const rows = Math.floor(height / SPACING)
  const offsetX = (width - cols * SPACING) / 2
  const offsetY = (height - rows * SPACING) / 2

  for (let i = 0; i <= cols; i++) {
    for (let j = 0; j <= rows; j++) {
      const nx = i * 0.1
      const ny = j * 0.1
      const value = Math.sin(nx + time * 0.5) * Math.cos(ny - time * 0.3) + Math.sin(nx * 0.5 - ny * 0.5 + time * 0.8)
      if (value <= 0.1) continue

      // A fixed scatter of blue and purple dots; the rest are slate, brighter where the waves overlap.
      const highlight = Math.sin(i * 12.34) * Math.cos(j * 56.78)
      ctx.fillStyle =
        highlight > 0.98
          ? '#3b82f6'
          : highlight < -0.98
            ? '#8b5cf6'
            : `rgba(148, 163, 184, ${Math.min(0.6, (value - 0.1) * 0.8)})`
      ctx.beginPath()
      ctx.arc(offsetX + i * SPACING, offsetY + j * SPACING, DOT_RADIUS, 0, Math.PI * 2)
      ctx.fill()
    }
  }
}

/** Fills its parent. Decorative only: not focusable, not clickable, hidden from screen readers. */
export default function LandingBackground() {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    let width = 0
    let height = 0
    let time = 0
    let last = performance.now()
    let frame = 0

    const resize = () => {
      width = canvas.width = canvas.offsetWidth
      height = canvas.height = canvas.offsetHeight
    }
    const tick = (now) => {
      // Capped so coming back to a background tab doesn't jump the pattern forward.
      time += 0.02 * SPEED * (Math.min(now - last, 100) / FRAME_MS)
      last = now
      drawFrame(ctx, width, height, time)
      frame = requestAnimationFrame(tick)
    }

    resize()
    window.addEventListener('resize', resize)
    frame = requestAnimationFrame(tick)
    return () => {
      window.removeEventListener('resize', resize)
      cancelAnimationFrame(frame)
    }
  }, [])

  return (
    <div
      aria-hidden
      inert
      className="pointer-events-none absolute inset-0 overflow-hidden"
      style={{ background: BACKGROUND, filter: FILTER }}
    >
      <canvas ref={canvasRef} className="block h-full w-full" />
    </div>
  )
}
