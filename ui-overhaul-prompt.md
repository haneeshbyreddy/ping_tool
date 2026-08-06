# Next session: the visual overhaul ("it's too bland")

This file IS the brief. Start a session with:
`Read ui-overhaul-prompt.md and follow it. Start at "Start here".`

---

## Who you are for this session

You are a UI/UX veteran who has spent a career on **operator-facing tools** — NOC
dashboards, observability, incident response, industrial SCADA. You have shipped
this exact category enough times to know it fails in exactly two directions:

- **The Grafana failure** — rainbow categorical palettes, neon glows, gauge
  skeuomorphism, twelve panel types on one screen. Looks technical, reads as
  noise, and nobody can tell an alarm from a legend.
- **The monotone failure** — every element muted "for restraint" until the whole
  product is one gray sheet and the operator's eye has nowhere to land.

**This product has the second one, and it got there honestly.** Read the design
history in `CLAUDE.md` (the map sections especially): round after round of
*dulling* — the basemap dulled three times in one day, plant muted, live drops
made "the quietest fill on the map," passive pins subordinated, labels pushed to
a legibility floor. Every individual decision was correct and well-argued. The
compound result is an app whose default resting state has almost no colour in it
at all.

Your job is **not** to relitigate any of those decisions. It is to add the axis
that was never built, so the app can be vivid and legible *without* touching the
alarm channel.

You know the difference between decoration and encoding. You do not add colour
because a screen looks empty. You add it because a colour is carrying a fact.

---

## The complaint, and the measurements behind it

The operator's words: **"too bland, things blend into the background."** They are
right, and it is measurable. Do not take my numbers on faith — re-measure them in
step 0.2 — but this is the shape of it.

**Dark mode neutrals** (`web/src/index.css`, `.dark` block):

| token | value | note |
|---|---|---|
| `--background` | `#0c0e12` | canvas |
| `--card` | `#1c1f24` | |
| `--popover` | `#24272e` | |
| `--accent` | `#2c3038` | |
| `--border` | `rgba(255,255,255,0.10)` | resolves to ~`#31353d` on card |

Three findings:

1. **The surface ladder sits at its documented floor.** `CLAUDE.md` says "keep
   adjacent surfaces >= ~3 ΔL*." Card→popover→accent are each about that. At the
   floor, a card does not read as an object — it reads as a slightly different
   patch of the same sheet.
2. **The border is nearly invisible.** ~`#31353d` against `#1c1f24` is roughly
   1.3:1. Panels have no edge, so nothing looks contained.
3. **On a healthy screen there is effectively zero chroma anywhere.** The
   neutrals carry a whisper of blue and nothing else. Status tones are the only
   real colour in the product and they are, correctly, reserved for failure. So
   the *default state of the application is colourless* — and "bland" is the
   accurate word for that.

**The unlock: you already own a good categorical palette and it never reaches the
app.** `--map-line-*` in `index.css` carries six genuinely vivid hues (violet
`#a98bef`, magenta `#dd7fc0`, teal `#4fc3b5`, lime `#b0c95f`, indigo `#8093f0`,
chalk `#d3d7df`) with real chroma, already closed, already validated as clear of
the status tones, already mirrored in `central/inventory.py:PALETTE`. It is
confined to map lines, tags, and probes. Nothing in the app chrome uses it.

**And a real bug worth fixing early:** `--chart-1..5` are currently just
`primary / success / warning / destructive / gray`. Every chart in this product
draws its series in **status colours**. A perfectly healthy series rendered in
`--destructive` red is a false alarm with a legend on it.

---

## The thesis: this product needs two colour axes, not one

Everything below follows from this. State it back to me before you start so I
know we agree.

**Axis A — STATUS. What it says: "is this broken?"**
Red / amber / green / info. Reserved, supreme, and **unchanged by this work**.
Every existing rule about it survives: it is the loudest thing on any screen, it
outranks decoration, a suppressed chip never means a suppressed page.

**Axis B — IDENTITY. What it says: "what kind of thing is this?"**
Categorical, always present, never alarming. Optical vs traffic vs health vs
plant vs probe vs subscriber. This axis **does not exist today** and building it
is the single biggest cure for blandness, because it puts colour on the ~95% of
the screen that is currently fine and therefore currently gray.

Plus two supporting moves:

**Accent** — one hue, used *only* for interaction: focus, selection, active nav.
`--primary` (`#74aec9`) already exists and is barely visible anywhere. Selection
today is a border-strong outline that does not read in a screenshot.

**Neutrals with more separation and a little more chroma.** Widen the ladder off
the floor and let the grays carry the brand hue. This is most of what makes
Linear / Vercel / Raycast dark modes look expensive rather than flat.

**Why this cures blandness without breaking anything:** the app gets substantially
more colour, but all of it lands on the identity axis, which by construction can
never be confused with an alarm.

---

## Non-negotiables

Break any of these and the work is a regression, however good it looks.

1. **Status tones stay the loudest chroma on any screen.** Concrete, checkable
   test: no Axis-B colour may exceed the screen's status tones in either chroma
   or contrast against its own background. Measure it, don't eyeball it.
2. **Axis B lives in hue 200°–330°** (blue → cyan → violet → magenta). It may
   **never** enter 20°–140° (red / amber / green territory). This is the rule
   that makes "more colour" safe, and it is mechanically verifiable.
3. **Green is not decoration.** The most common overcorrection to "too bland" is
   painting healthy things green. That destroys the alarm channel exactly as
   thoroughly as painting everything red does. Healthy is *neutral*.
4. **Every honesty rule in `CLAUDE.md` survives.** Unmeasured is not zero and not
   green; frozen looks frozen and says why; stale says it is stale; a dash is not
   a reading. If anything, this pass should make them *more* visible, not less.
5. **The map's rank system is not reopened.** Rounds 1–11 are settled. You may
   inherit its method; you may not re-tune its numbers. The one thing you owe it
   is a regression check at the end.
6. **New tokens must reach the superadmin Appearance panel or be deliberately
   excluded.** `central/theme.py:_TOKENS` and the SPA's `ALL_TOKENS` are two
   hand-kept lists and `test_allowlist_matches_spa` pins them. Cartography tokens
   (`--map-line-*`, `--map-live*`) are deliberately excluded; decide consciously
   which side each new token falls on.
7. **Both themes, in a real browser.** Dark is the focus and the priority, but
   light must not break. `CLAUDE.md` records two separate occasions where a theme
   change passed static review and unit tests and was broken on screen.

---

## The plan

**Work the list below one item at a time.** Maintain it as a todo list. After
every numbered item, stop and show me what changed before starting the next one.
Do not batch. Do not run ahead. If an item turns out to be wrong once you are
inside it, say so and stop rather than improvising a different item.

### Phase 0 — Measure before you touch anything (no code)

- **0.1** Stand up the verification environment first: second central on a **copy**
  of prod's DB per the browser-verify recipe, with the `app_settings` WhatsApp
  rows wiped. Confirm you can screenshot the SPA without deploying.
- **0.2** Build the **chroma and contrast inventory** for dark mode: every surface,
  border, and text token, as OKLCh L/C/H plus contrast against what it actually
  sits on. Include the status tones and `--map-line-*`. This table is the evidence
  base for every later decision. Confirm or correct my three findings above.
- **0.3** Run a **rank audit** on Home, Issues, Network, and the device panel.
  Four ranks — verdict / state / reference / chrome. Flag every place a lower rank
  currently outranks a higher one.
- **0.4** Inventory **what domain object kinds exist** and would want an identity
  hue: optical, traffic/bandwidth, health/vitals, plant/passive, probe/edge,
  subscriber/drop, port, PON. Propose the grouping; do not assign colours yet.

**GATE — show me 0.2, 0.3, 0.4 as three tables. No code until I have read them.**

### Phase 0.5 — Reference research (REQUIRED, and it is not optional polish)

**Read the "why generic happens" section below before starting this phase.** The
single largest cause of template-looking output is designing from defaults with
no external reference. You do not get a distinctive product by thinking harder in
an empty room. You get it by studying what shipped, extracting the specific move,
and porting it into this product's own grammar.

The deliverable for this phase is a **swipe file**: one table, roughly 25–40 rows,
in the shape

| source | the specific move | why it works | where it maps here |
|---|---|---|---|

A row must name a *move*, not a screen. "Linear's issue list looks good" is
useless. "Linear renders the selected row with a 1px inset ring in the accent hue
plus a 4% accent fill, so selection survives on every surface without a coloured
left rail" is a row you can build from.

- **0.5.1 — Mobbin.** Search by **app name and UI element**, never by category.
  The "Dashboard" and "Analytics" categories are dominated by consumer SaaS and
  marketing-adjacent design, which is where the generic look comes from.
  - Apps worth studying for this product: **Linear** (list rows, selection,
    restraint, keyboard surfaces), **Vercel** (dark type, elevation, empty
    states), **Raycast** (dark surface craft, command grammar), **Sentry** (issue
    streams at scale, grouping, per-row microviz), **Retool** and **Height**
    (dense operator surfaces), **Stripe** (number formatting, tables, tabular
    figures).
  - **The sharpest analogue is dark fintech and trading** — Mercury, Coinbase
    Advanced, trading terminals. Structurally identical to a NOC: dense dark,
    real-time numbers, red and green *reserved for direction*, tabular figures,
    sparklines, watchlists, a status ribbon. Nobody in the network-tools category
    looks there and it shows.
  - Extract: selection states, row density, how a number and its delta sit
    together, how chips are weighted, how empty and loading states are handled,
    how filter bars are built. **Do not extract**: page composition, hero
    treatments, type scale, whitespace rhythm. Those are tuned for a consumer
    audience on a phone and are wrong here.

- **0.5.2 — 21st.dev.** We are on shadcn, so these are structurally compatible —
  which is exactly why the failure mode is assembly rather than design. **Read the
  source for technique; port the technique into the existing `wisp-*` primitives;
  never install.** The value is in the CSS craft: how a component builds an
  elevated surface, an inset highlight, a gradient hairline border, a chip, a
  shimmer, a table hover, a focus ring. That craft is the difference between
  "flat gray boxes" and something that looks made.
  - Mine especially for **micro-interaction and motion**, which this app has
    almost none of and which is a large part of why it reads as static and bland.
  - **Refuse outright** anything that ships its own shadow scale, radius scale,
    colour palette, or animation library, and every marketing-page component
    (heroes, pricing tables, testimonial carousels, bento grids). That is most of
    any registry and all of it is wrong for a NOC.

- **0.5.3 — The instrument well. This is the differentiator, so spend real time
  here.** This product's domain has its own visual tradition and no SaaS template
  has any of it: **OTDR traces, optical power meters, spectrum analyzers, patch
  panel and rack elevation diagrams, Bloomberg Terminal, flight decks, oscilloscope
  and logic analyzer displays.** A fiber tech reads an OTDR every week. A tool that
  borrows its grammar from test equipment reads as credible to that person in a way
  that borrowing from Notion never will — and it is unimitatable, because nobody
  else in this category is looking there.
  - Concretely: the Rx dBm mini-scale (item 3.3) should look like a power meter,
    not like a progress bar. The PON heat strip is already halfway to a channel
    display. Ranging distance wants a trace, not a number.

- **0.5.4** — Get trial accounts and screenshot the real thing where you can:
  **Sentry, Datadog, Grafana Cloud, Cloudflare, Vercel, Linear, Mercury.** A live
  product at your own viewport with your own eyes beats a cropped screenshot.

**GATE — show me the swipe file. I want to see the specific moves before you
design anything, and I will push back on any row that is a vibe rather than a
mechanism.**

### Phase 1 — Design the system on paper

Everything in this phase must cite the swipe file. If a decision has no row behind
it and no measurement behind it, it is taste, and taste in an empty room is how
generic output happens.

- **1.1** Assign Axis B hues to the object kinds from 0.4, drawing on the existing
  `--map-line-*` set wherever it fits. State the hue angle for each and prove
  every one is inside 200°–330°.
- **1.2** Write the **budget table**: for each context, the maximum chroma and
  contrast an Axis-B element may take, derived from the status tone it shares a
  screen with. This is the artifact that keeps the rule enforceable later.
- **1.3** Redesign the neutral ladder: wider steps, slightly more chroma. Note the
  constraint from `CLAUDE.md` — the canvas is already near the halation floor, so
  **raise card and popover rather than deepening background**. Give `--border` a
  real edge.
- **1.4** Fix `--chart-1..5`: an Axis-B categorical ramp, not the status tones.
- **1.5** Define exactly where the accent is allowed: focus ring, selected row,
  active nav, and nothing else.

**GATE — show me the full system as one spec table, both modes, with the
before/after numbers. This is the decision point. No code until approved.**

### Phase 2 — Foundations in code

Each of these is a separate item with its own before/after screenshot.

- **2.1** Neutral ladder, surfaces, borders.
- **2.2** Axis-B tokens and the chart ramp.
- **2.3** `font-variant-numeric: tabular-nums` on every changeable number. Invert
  the stat-tile emphasis (label readable, number ~1.5–2× body, drop the uppercase
  letterspaced eyebrow as the default).
- **2.4** One inner top highlight on raised surfaces
  (`inset 0 1px 0 rgb(255 255 255 / 0.04)`). No drop shadows — the surface-step
  approach is correct for dark.
- **2.5** Accent on focus / selection / active nav.

**GATE after 2.5 — full screenshot set, dark and light, before vs after.**

### Phase 3 — Component grammar

- **3.1** Extend the `status-badge.tsx` tone formula (text + ~13% fill + 30% edge)
  to Axis-B identity chips, so an identity chip and a status chip sit at the same
  structural weight but can never be mistaken for one another.
- **3.2** Build the **`<Reading>` primitive** — one component for every number that
  can be uncertain, with a distinct visual form per epistemic state:
  measured-current / measured-stale / frozen / not-measured / suppressed. This is
  the highest-value item in the whole plan; see "the non-generic move" below.
- **3.3** Rx dBm as a **mini scale**, not a bare number: a short track showing
  where the reading sits between that OLT's own warn and crit thresholds. An ISP
  tech reads optical power on a scale; render it the way they think about it.
- **3.4** Identity left-rail on list rows (extend the existing tag-colour rail).
- **3.5** A sparkline primitive fed from `rollup.py`'s hourly buckets. A number
  with no trend is half a number.

**GATE per item.**

### Phase 4 — Screens, one at a time, each its own gate

- **4.1 Home.** Three bands: one verdict line at the top (largest thing on the
  page), then only the tiles that are actually saying something, then context.
  Tiles reading zero collapse into a single quiet strip. **Fix the credibility
  bug**: "All clear" currently renders directly under 40 critical and 93 warning
  ONUs. Either the verdict covers everything on the page, or it drops the words
  "All clear."
- **4.2 Issues.** Group by device → PON with an aggregate header; delete the
  severity badge column (in a filtered view the badge is the filter chip); move
  the repeated region into the group header; right-align the Rx value as the only
  toned thing in the row. 25 critical ONUs on one OLT is not 25 problems.
- **4.3 Network.** List-first, dense aligned columns. Cards destroy the column
  alignment that comparison depends on, and 18 devices is a comparison task.
- **4.4 Device panel.** Mostly working. The PON heat strip is this product's
  signature mark — repeat it into the Network row, the map hover card, and the
  Issues group header.
- **4.5 Map.** Regression check only. Confirm rounds 1–11 still hold under the new
  neutrals, especially the basemap contrast ceilings.

### Phase 5 — Lock it in

- **5.1** Sync `theme.py:_TOKENS` and `ALL_TOKENS`; make the allowlist test pass.
- **5.2** Write the new colour system into `CLAUDE.md` in the house style — the
  rule plus the reason it exists, with the measurements.
- **5.3** `.venv/bin/python -m unittest discover -s tests` and `tsc -b`.
- **5.4** Browser-verify both modes on the copy DB.
- **5.5** Only then deploy.

---

## The non-generic move (item 3.2, expanded)

This is what will separate the result from a shadcn template, and it is the one
part no competitor can copy, because it comes from the domain rather than from
taste.

This product's honesty rules are already its most distinctive property —
*"nothing is wrong" and "nothing is measured" must never render alike*, a frozen
reading must look frozen, a stale walk prints nothing, a dash is not a zero.
That grammar exists in prose and in scattered code. **It has no consistent visual
form.** Give it one:

| state | treatment |
|---|---|
| measured, current | solid fill, full ink |
| measured, stale | value shown, hatched or dotted underline, age adjacent |
| frozen (upstream down) | desaturated, pause glyph, reason beside it |
| not measured (capability gap) | em dash on a hairline placeholder — never zero, never green |
| suppressed (governor) | state renders, bell struck through |

One primitive, used everywhere a number can be uncertain. Do that and the app
stops looking like a dashboard and starts looking like an **instrument**.

---

## Anti-patterns — do not do these

- **Do not paint healthy things green.** Healthy is neutral. See non-negotiable 3.
- **No glows, neon, or gradients on surfaces.** The "cyberpunk NOC" look reads as
  a toy to people who run networks for a living.
- **No rainbow.** Axis B should be about five or six hues, all cool, all related.
  Twelve categorical colours is Grafana.
- **Do not add colour to fill space.** If a region looks empty, the fix is content
  or measure, not paint.
- **Do not *install* from a registry — port from it.** 21st.dev is required
  reading (0.5.2) and pasted components are forbidden. The five `wisp-*`
  primitives exist specifically to stop per-page Tailwind drift, and a pasted
  component brings its own radius, spacing, and shadow opinions with it.
- **Do not take page composition from Mobbin — take moves from it.** Its corpus
  skews consumer, so its layouts, type scale, and whitespace rhythm are wrong
  here. Its selection states, row density, chip weighting, and number treatments
  are exactly right. Study apps by name (0.5.1), never browse the Dashboard
  category.
- **Do not shrink things to subordinate them.** `CLAUDE.md` records this failing
  three times. Subordination is tone, stacking, and zoom floors.

---

## Why generic happens (the operator has said this many times — internalise it)

"Generic" is not a taste failure. It is a **process** failure with identifiable
causes. Every one of these produces template-looking output regardless of how
good the individual decisions are:

1. **Designing from defaults.** Untouched shadcn radius, spacing, and shadow, plus
   a Tailwind palette, is a recognisable look. Thousands of apps share it.
2. **Designing with no external reference.** An empty room produces the average of
   what you have seen. The average is generic by definition. This is why Phase 0.5
   is mandatory and comes before Phase 1.
3. **Assembling instead of designing.** Registry components each carry their own
   opinions; twelve of them on a page is twelve design systems arguing.
4. **Symmetry as a substitute for hierarchy.** Equal-weight card grids and 3-column
   feature rows are what you produce when you have not decided what matters.
5. **Decorative colour.** Colour that encodes nothing reads as styling, and styling
   reads as a template. Every hue in this pass must carry a fact.
6. **Ignoring the domain's own visual tradition.** This is the big one, and it is
   the reason 0.5.3 exists. Networking and optics have a century of instrument
   design behind them. A tool that draws on it cannot look like a CRM.

### The generic tells — check the result against this list

If more than two of these are true at the end, the pass has failed:

- Uppercase letterspaced micro-labels used as the default eyebrow
- Giant number over tiny label as the stat tile pattern
- Equal-weight card grid as the default layout
- One border radius on everything
- A purple-to-blue gradient anywhere
- Emoji standing in for icons
- Centred empty state with a generic illustration
- Colour present only where something is broken, or only as decoration
- Charts drawn in status colours
- Numbers without tabular figures
- Selection indicated only by a border you cannot see in a screenshot
- Nothing on screen that a competitor could not have shipped

---

## Repo facts you need

- **`npm run build` in `web/` DEPLOYS LIVE.** Verify through a second central on a
  copy of prod's DB. Wipe the WhatsApp rows in `app_settings` first — a local
  central has WhatsApp'd real customers before.
- **`tsc --noEmit` checks nothing here. Use `tsc -b`.**
- **Tests: `.venv/bin/python -m unittest discover -s tests`.** Never bare
  `python3` — it lacks httpx and fakes about 12 failures.
- Backend changes need a **central restart**; SPA changes need a build.
- Colours are operator-settable: **check Settings → Platform → Appearance before
  editing `index.css`**. `index.css` remains the shipped default and the design
  record.
- Baseline is dirty and there is a queue of undeployed work in `MEMORY.md`. Read
  it and confirm what state the tree is in before you start.
- Copy voice: **no em-dashes in user-visible prose.** Bare `—` as a null
  placeholder is deliberate and stays.

---

## Start here

Read `CLAUDE.md`'s theme and map sections, then `web/src/index.css` end to end,
then confirm:

1. Your understanding of the two-axis thesis, in your own words.
2. Whether my three measurements in "the complaint" hold up.
3. Your reading of "why generic happens", and which of the six causes this
   codebase is currently most exposed to.
4. Your todo list for Phase 0 and Phase 0.5.

Then do item 0.1 and stop.

**One standing instruction for the whole session.** The operator has said "I do
not want generic design" more times than any other requirement in this project.
Treat it as the acceptance criterion, not as a preference. Before every gate, ask
the last question on the tells list — *could a competitor have shipped this?* — and
if the answer is yes, the item is not done.
