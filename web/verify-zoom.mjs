// THROWAWAY: high-DPI close-ups of one subscriber pin and one splitter pin,
// plus the name plate's real offset from the pin's tip.
import { chromium } from "playwright"
import fs from "node:fs"
const BASE = "http://localhost:5199"
const OUT = process.env.OUT || "/tmp/verify-shots"
fs.mkdirSync(OUT, { recursive: true })

for (const theme of ["dark", "light"]) {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({
    viewport: { width: 1400, height: 900 }, deviceScaleFactor: 4,
  })
  await ctx.addInitScript((t) => {
    localStorage.setItem("wisp-central-theme", t)
    localStorage.setItem("wisp:map:ref-onus", "on")
  }, theme)
  const page = await ctx.newPage()
  await page.request.post(`${BASE}/api/login`, {
    data: { username: "verifyowner", password: "Verify-Pass-123!", remember: true } })
  await page.goto(`${BASE}/app/#/map?onu=${encodeURIComponent("74:88:2A:FA:07:F8")}`)
  await page.waitForSelector(".wisp-map-wrap", { timeout: 20000 })
  await page.waitForTimeout(5000)
  try { await page.locator("button:has(svg.lucide-x)").first().click({ timeout: 2000 }) } catch {}
  await page.waitForTimeout(500)
  for (let i = 0; i < 1; i++) {
    await page.locator(".leaflet-control-zoom-in").click().catch(() => {})
    await page.waitForTimeout(900)
  }
  await page.waitForTimeout(1500)

  // Where each mark, its tip and its name plate actually land.
  const geo = await page.evaluate(() => {
    const out = { subs: [], splitters: [],
      counts: { refs: document.querySelectorAll(".wisp-refonu").length,
                pins: document.querySelectorAll(".wisp-pin").length,
                spl: document.querySelectorAll(".wisp-pin--t-splitter").length },
      firstRef: (() => { const w = document.querySelector(".wisp-refonu")
        if (!w) return null
        const r = w.parentElement.getBoundingClientRect()
        return { x: +r.left.toFixed(0), y: +r.top.toFixed(0) } })() }
    for (const w of document.querySelectorAll(".wisp-refonu")) {
      const a = w.parentElement.getBoundingClientRect()
      const m = w.querySelector(".wisp-refonu__mark").getBoundingClientRect()
      if (a.top < 80 || a.top > 820 || a.left < 290 || a.left > 1340) continue
      out.subs.push({ x: +a.left.toFixed(1), y: +a.top.toFixed(1),
                      tipOff: +(m.bottom - a.top).toFixed(2),
                      cls: w.className })
    }
    for (const nm of document.querySelectorAll(".wisp-refonu-name")) {
      const a = nm.parentElement.getBoundingClientRect()
      const r = nm.getBoundingClientRect()
      out.subs.forEach((s) => {
        if (Math.abs(s.x - a.left) < 1 && Math.abs(s.y - a.top) < 1)
          s.nameTopBelowTip = +(r.top - a.top).toFixed(2)
      })
    }
    for (const p of document.querySelectorAll(".wisp-pin--t-splitter")) {
      const a = p.parentElement.getBoundingClientRect()
      const d = p.querySelector(".wisp-pin__dot").getBoundingClientRect()
      const l = p.querySelector(".wisp-pin__label").getBoundingClientRect()
      if (a.top < 60 || a.top > 850 || a.left < 300 || a.left > 1350) continue
      out.splitters.push({ x: +a.left.toFixed(1), y: +a.top.toFixed(1),
        dotTopAbove: +(a.top - d.top).toFixed(2),
        tipAbove: +(a.top - d.bottom).toFixed(2),
        labelTopBelowTip: +(l.top - d.bottom).toFixed(2), cls: p.className })
    }
    return out
  })
  console.log(theme, JSON.stringify(geo, null, 1))

  const shoot = async (name, pt) => {
    if (!pt) return
    await page.screenshot({ path: `${OUT}/${theme}-zoom-${name}.png`,
      clip: { x: Math.max(0, pt.x - 70), y: Math.max(0, pt.y - 60),
              width: 170, height: 120 } })
    console.log("zoom", theme, name)
  }
  await shoot("subscriber", geo.subs[0])
  await shoot("subscriber2", geo.subs[1])
  await shoot("splitter", geo.splitters[0])
  await browser.close()
}
