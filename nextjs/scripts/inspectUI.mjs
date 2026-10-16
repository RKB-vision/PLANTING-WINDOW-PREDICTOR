import { chromium } from 'playwright'
import { mkdirSync } from 'fs'

const BASE = process.env.APP_URL || 'http://localhost:3000'
const OUT_DIR = 'ui-audit'

mkdirSync(OUT_DIR, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })

const response = await page.goto(BASE, { waitUntil: 'networkidle' })
console.log('HTTP status:', response.status())

await page.screenshot({ path: `${OUT_DIR}/desktop.png`, fullPage: true })

// Trigger a species prediction and capture the result area
try {
  await page.click('text=Use latest')
  await page.click('text=Predict for')
  await page.waitForSelector('text=Prediction Result', { timeout: 10000 })
  await page.screenshot({ path: `${OUT_DIR}/prediction.png`, fullPage: true })
} catch (error) {
  console.error('Prediction interaction failed:', error)
}

// Mobile viewport
await page.setViewportSize({ width: 390, height: 844 })
await page.waitForTimeout(500)
await page.screenshot({ path: `${OUT_DIR}/mobile.png`, fullPage: true })

// Report layout info for main sections
const info = await page.evaluate(() => {
  const out = {}
  const grab = (sel) => {
    const el = document.querySelector(sel)
    if (!el) return null
    const r = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    return {
      x: Math.round(r.x), y: Math.round(r.y),
      width: Math.round(r.width), height: Math.round(r.height),
      padding: cs.padding, margin: cs.margin,
      border: cs.border, borderRadius: cs.borderRadius,
      color: cs.color, background: cs.backgroundColor,
      display: cs.display, overflow: cs.overflow
    }
  }
  out.header = grab('header')
  out.selectSection = grab('section:nth-of-type(1)')
  out.select = grab('select')
  out.predictButton = grab('main button')
  out.regButton = grab('main button + button')
  out.result = grab('section:nth-of-type(2)')
  out.register = grab('section:nth-of-type(3)')
  return out
})

console.log('Layout snapshot:', JSON.stringify(info, null, 2))
await browser.close()
