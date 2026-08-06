// THROWAWAY browser check for the site hover card + location-pin marks.
// Runs from inside web/ because Playwright lives in web/node_modules.
import { chromium } from "playwright"
import fs from "node:fs"

const BASE = "http://localhost:5199"
const OUT = process.env.OUT || "/tmp/verify-shots"
fs.mkdirSync(OUT, { recursive: true })

const PDVR_MAC = "A0:7E:10:00:7A:51"   // HCP_KSFLEXY — on SPL-PDVR-A (all online)
const NDN_MAC = "4C:AE:1C:20:24:41"    // HCN_BANUZEROX — on SPL-NDN-C (2 dark)

async function run(theme) {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 },
    deviceScaleFactor: 3 })
  await ctx.addInitScript((t) => {
    localStorage.setItem("wisp-central-theme", t)
    localStorage.setItem("wisp:map:ref-onus", "on")
  }, theme)
  const page = await ctx.newPage()
  const msgs = []
  page.on("console", (m) => { if (m.type() === "error") msgs.push(m.text()) })
  page.on("pageerror", (e) => msgs.push("PAGEERROR " + e.message))

  const r = await page.request.post(`${BASE}/api/login`, {
    data: { username: "verifyowner", password: "Verify-Pass-123!", remember: true },
  })
  if (!r.ok()) throw new Error("login failed " + r.status() + await r.text())

  const shot = async (name) => {
    await page.screenshot({ path: `${OUT}/${theme}-${name}.png` })
    console.log("shot", theme, name)
  }
  // Marks exist in the DOM for the whole org; most are off-screen. Pick one that
  // is actually in view, optionally skipping the pin a deep link selected.
  const onScreen = async (sel, skipSelected) => {
    const loc = page.locator(sel)
    const n = await loc.count()
    for (let i = 0; i < n; i++) {
      const el = loc.nth(i)
      const b = await el.boundingBox()
      if (!b || b.x < 260 || b.y < 60 || b.x > 1500 || b.y > 900) continue
      if (skipSelected && (await el.getAttribute("class") || "").includes("--selected")) continue
      return el
    }
    return null
  }

  const cardText = async () =>
    (await page.locator(".wisp-mapcard__box").count())
      ? (await page.locator(".wisp-mapcard__box").first().innerText()).replace(/\n/g, " | ")
      : "(no card)"

  // The ?onu= deep link is the only reliable way to move the map from a script:
  // it enables the subscriber layer, flies past both zoom floors and selects the
  // pin. react-leaflet keeps no map handle on the DOM.
  const flyOnu = async (mac) => {
    await page.goto(`${BASE}/app/#/map?onu=${encodeURIComponent(mac)}`)
    await page.waitForSelector(".wisp-map-wrap", { timeout: 20000 })
    await page.waitForTimeout(4500)
    // Close the subscriber panel the deep link opened — it covers a third of the
    // map, and a selected pin suppresses its own hover card by design.
    const x = page.locator("aside button, .absolute button").filter({ has: page.locator("svg") })
    try { await page.locator('button:has(svg.lucide-x)').first().click({ timeout: 2000 }) }
    catch { await page.keyboard.press("Escape") }
    await page.waitForTimeout(600)
    // …and get close enough for the marks to be judged as marks.
    for (let i = 0; i < 2; i++) {
      await page.locator(".leaflet-control-zoom-in").click().catch(() => {})
      await page.waitForTimeout(900)
    }
    await page.waitForTimeout(600)
  }

  // A tight crop around a mark — a full 1600px map screenshot renders these at
  // 11px and no shape judgement is possible from it.
  const closeUp = async (name, sel) => {
    const el = await onScreen(sel, false)
    if (!el) { console.log(theme, "no on-screen", sel); return }
    const b = await el.boundingBox()
    const cx = Math.max(240, Math.min(1340, b.x)), cy = Math.max(60, Math.min(700, b.y))
    await page.screenshot({ path: `${OUT}/${theme}-${name}.png`,
      clip: { x: cx - 90, y: cy - 65, width: 220, height: 150 } })
    console.log("closeup", theme, name)
  }

  // ---- 1. the SITE badge and its hover card -----------------------------
  await page.goto(`${BASE}/app/#/map`)
  await page.waitForSelector(".wisp-map-wrap", { timeout: 20000 })
  await page.waitForTimeout(4500)
  const badges = page.locator(".wisp-cluster")
  const n = await badges.count()
  let best = 0, bestN = 0
  for (let i = 0; i < n; i++) {
    const v = parseInt(await badges.nth(i).locator(".wisp-cluster__n").innerText(), 10)
    if (v > bestN) { bestN = v; best = i }
  }
  console.log(theme, `badges=${n} biggest=${bestN}`)
  await badges.nth(best).hover({ force: true })
  await page.waitForTimeout(800)
  console.log(theme, "SITE CARD:", await cardText())
  await shot("1-site-card")

  // the cables into every member should light while the badge is hovered
  const lit = await page.evaluate(() =>
    [...document.querySelectorAll("path.leaflet-interactive, .leaflet-overlay-pane path")]
      .filter((p) => Number(p.getAttribute("stroke-opacity")) === 1).length)
  console.log(theme, "links at full opacity while hovering the badge:", lit)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(400)

  // ---- 2. subscriber pins + splitters (PDVR) ----------------------------
  await flyOnu(PDVR_MAC)
  console.log(theme, "marks:", await page.locator(".wisp-refonu").count(),
              "pins:", await page.locator(".wisp-pin").count(),
              "splitters:", await page.locator(".wisp-pin--t-splitter").count())
  await shot("2-subscribers-pdvr")
  await closeUp("2b-marks-closeup", ".wisp-refonu")
  await closeUp("2c-splitter-closeup", ".wisp-pin--t-splitter")

  const shapes = await page.evaluate(() => {
    const get = (sel) => {
      const el = document.querySelector(sel)
      if (!el) return null
      const cs = getComputedStyle(el)
      return { w: cs.width, h: cs.height, r: cs.borderRadius,
               t: cs.transform, bg: cs.backgroundColor }
    }
    const out = {
      subscriber: get(".wisp-refonu__mark"),
      splitter: get(".wisp-pin--t-splitter .wisp-pin__dot"),
      tones: {},
    }
    for (const k of ["ok", "weak", "dark", "quiet"])
      out.tones[k] = document.querySelectorAll(`.wisp-pin--drops-${k}`).length
    // a splitter with NO drop class at all is the `quiet` case
    out.tones.untoned = [...document.querySelectorAll(".wisp-pin--t-splitter")]
      .filter((p) => !p.className.includes("drops-")).length
    return out
  })
  console.log(theme, "SHAPES", JSON.stringify(shapes))

  // is the pin's TIP on its coordinate?
  const geom = await page.evaluate(() => {
    const wrap = document.querySelector(".wisp-refonu")
    if (!wrap) return null
    const a = wrap.parentElement.getBoundingClientRect()   // anchor = the latlng
    const m = wrap.querySelector(".wisp-refonu__mark").getBoundingClientRect()
    const W = m.width
    return {
      // getBoundingClientRect gives the AABB of the ROTATED square, whose
      // bottom edge the sharp corner touches exactly — so m.bottom IS the tip.
      markAboveAnchor: +(a.top - m.top).toFixed(2),
      tipVsAnchor: +(m.bottom - a.top).toFixed(2),
      side: +(W / Math.SQRT2).toFixed(2),
    }
  })
  console.log(theme, "GEOM", JSON.stringify(geom))

  // hover a subscriber: card + solid drop line
  const sub = await onScreen(".wisp-refonu", true)
  if (sub) {
    await sub.hover({ force: true })
    await page.waitForTimeout(800)
    console.log(theme, "SUBSCRIBER CARD:", await cardText())
    await shot("3-subscriber-hover")
    await page.mouse.move(5, 5)
    await page.waitForTimeout(300)
  }

  // hover a splitter: its own card
  const spl = await onScreen(".wisp-pin--t-splitter", false)
  if (spl) {
    await spl.hover({ force: true })
    await page.waitForTimeout(800)
    console.log(theme, "SPLITTER CARD:", await cardText())
    await shot("4-splitter-hover")
    await page.mouse.move(5, 5)
    await page.waitForTimeout(300)
  }

  // ---- 3. the DARK splitter (NDN) ---------------------------------------
  await flyOnu(NDN_MAC)
  await shot("5-ndn")
  await closeUp("5b-ndn-closeup", ".wisp-pin--drops-dark")
  const dark = await onScreen(".wisp-pin--drops-dark", false)
  if (dark) {
    await dark.hover({ force: true })
    await page.waitForTimeout(800)
    console.log(theme, "DARK SPLITTER CARD:", await cardText())
    await shot("6-dark-splitter")
  } else {
    console.log(theme, "NO dark splitter drawn")
  }

  console.log(theme, "console errors:", msgs.slice(0, 5))
  await browser.close()
}

await run("dark")
await run("light")
