# Next session: deploy the reference-point split, then verify it in a browser

## Where this came from

The map was drawing loud red dotted lines to two customers (SRIKRISHNA TIMBER,
DHARMATEJA.S). The documented rule is that an offline customer gets **only a red
dot** — everything louder is reserved for a WITNESS, i.e. a subscriber whose
power the operator has vouched for, because power cannot explain a dark witness
and `ponfault` therefore calls a fibre cut and rolls a splicing crew.

The map was right. Those two were `witness=1`, and so were 37 other field
captures that nobody had deliberately vouched for.

**The cause:** `POST /api/inventory/onu-place` passed no `witness` and took
`store.set_onu_place`'s default of `True`. So every desktop write asserted the
power claim — moving a pin, editing a phone number, the map card's "Move"
button. Field captures landed correctly at `witness=0` via `/survey` and were
flipped seconds later by whatever desktop action followed. On badri_fiber that
was 30 customers in one morning; DB timestamps show `updated_at` a few seconds
after `placed_at` on every one of them.

**The operator's call (2026-08-04):** *"I want an explicit toggle to make it a
reference, and even if I were to add location from the network page it still
should be a normal customer point."* Plus: clear every existing claim.

## What is already done (uncommitted, NOT deployed)

### Backend

- **`inventory.clean_onu_place_payload` has no `witness` key at all** — unsayable,
  the same way `clean_field_onu_payload` has never been able to spell it. An
  earlier cut made it an explicit optional key that preserved when absent; that
  was still too clever, because a route that *can* carry the claim is a route
  somebody wires it back into.
- **`api/devices.onu_place`** reads the existing flag off the record and passes
  it straight back through, so a location write can neither promote nor demote.
- **`store.set_onu_place(witness=...)` is now a REQUIRED keyword** — the `True`
  default is gone, so no future caller can assert the claim by forgetting.
- **New `POST /api/inventory/onu-witness`** (`api/devices.onu_witness` →
  `store.set_onu_witness`, `inventory.clean_onu_witness_payload`): the claim
  alone. Owner-only, off the worker allowlist, UPDATE-only (404 on an unrecorded
  subscriber), prunes a row the claim was the only content of, touches no
  coordinate or provenance. Registered in `api/__init__.py`.
- **`/api/inventory/optics` ships `place.witness`** as a real boolean, so the
  Optical tab can tell the two claims apart.
- Unchanged and deliberate: `clear_onu_place_coords` still retracts the claim
  along with the pin.

### SPA

- **`reference-onu.tsx`** — the pin icon has three states (filled primary =
  reference point · muted = on the map, not vouched for · faint = no record); it
  keyed on `place != null` before, which is why every surveyed customer looked
  like a reference point. The dialog is now **"Subscriber location"**: Save
  writes location + contact only, and the claim is an explicit **Switch** that
  fires its own mutation on the click. The contract paragraph shows while the
  switch is OFF and stands down once the claim is made. The switch is disabled
  with an explanatory line until the subscriber has a record — and a FIRST
  placement therefore leaves the dialog open, so the refreshed row enables the
  switch in place instead of forcing save → reopen → flip.
- **`subscriber-detail.tsx`** — a `ReferenceToggle` in the actions row
  (Reference / Withdraw) behind a confirm dialog carrying the same contract
  wording. "Move"'s tooltip now says only the pin moves.
- **`map-page.tsx`** — `placingOnu` carries no claim; the placement banner no
  longer restates the power contract (it rode every plain move before, which is
  how the warning that matters stops being read).
- **`splitter-panel.tsx`** — same three-state pin marker.
- **`api.ts`** — `setOnuPlace` has no `witness` param; new `setOnuWitness`.
- **`types.ts`** — `OnuOptic.place` gains `witness: boolean`.

### Tests

- **New `tests/integration/test_central_witness.py`** (14 tests): the regression
  verbatim, the location route refusing to be talked into a claim even when a
  client sends one, claim/withdraw preserving provenance, a claim needing no pin,
  404 on an unrecorded subscriber, husk pruning, boolean validation, worker 403,
  org isolation, `place.witness` on the optics row, unpinning still retracting.
- Updated where they pinned the old default: `test_central_survey`
  (`test_the_owners_reference_dialog_still_means_witness` now goes through the new
  verb), `test_central_onuplaces` (3), and explicit `witness=True` at every
  `set_onu_place` call site in `test_central_ponalert` / `test_central_subscriber`
  / `test_store_migrations`.

### Production data — ALREADY DONE

All **42** witness claims cleared (`badri_fiber` 39, `byreddy` 2, `MS-Telecom` 1).
Every row survives as a plain located subscriber — pins, names, numbers,
provenance untouched; no husks needed pruning.

Backup + undo script (regenerate if the scratchpad is gone — it is session-scoped):
`witness-backup-2026-08-04.json` and `witness-restore.sql`.

## What is LEFT — start here

### 1. Deploy (both halves together, and they must go together)

```bash
cd web && npm run build
```

Then restart central. **Do not ship one without the other.** The SPA alone would
render every reference point as "not a reference point" (the old backend doesn't
send `place.witness`) and every Switch/Withdraw would 404 on the missing route.

**Until central restarts, the bug is still live** — the running process still
forces `witness=True` on every `onu-place`, so any pin moved on the desktop
re-promotes that customer and partly undoes the reset above. Worth telling the
operator not to touch pins until the restart.

### 2. Run the full suite

```bash
.venv/bin/python -m unittest discover -s tests
```

**Already green at the end of the session: 1766 tests, OK** (baseline was 1751;
+15 is the new witness file). Six tests failed along the way, all of them pinning
the old default, and all six were updated. `cd web && npx tsc -b` clean.

So this step is really just a re-confirmation after any further edits — nothing
is known-broken.

### 3. Browser-verify (not done at all)

Per `browser-verify-recipe` — second central on a COPY of prod's DB plus a vite
dev proxy, so verifying never deploys. Check:

- Optical tab: a surveyed customer shows a MUTED pin, a vouched-for one FILLED
  primary, an unrecorded one faint.
- The dialog's Switch actually flips and the toast fires; the contract paragraph
  appears when OFF and stands down when ON; the switch is disabled with its
  explanation on an unplaced ONU.
- Subscriber card (map): Reference / Withdraw round-trips.
- Map: a red dotted drop line appears ONLY for a dark customer you have vouched
  for. Everything else offline = red dot only.
- **Both themes.** This layer has twice shipped unreadable over satellite tiles.

### 4. Then commit

Nothing here is committed. The working tree also carries a lot of earlier
uncommitted work (map detail settings, subscriber-as-one-object, Traccar worker
tracking, the hover card) — see `MEMORY.md`. Decide whether this lands as its own
commit or with that batch.

## Also found, deliberately NOT fixed (there is a task chip for it)

`web/src/routes/map-page.tsx:~1328` calls `useResizablePanel` AFTER the
`if (!scopeOrg) return <NeedsOrg />` guard at ~1267. Conditional hook — a
superadmin loading `/map` with no org scope and then selecting one will hit
"Rendered more hooks than during the previous render" and the map goes blank.
It is the only lint ERROR in the repo (`npm run lint`); everything else is a
warning. Pre-existing uncommitted work, unrelated to this change. Fix is to move
the hook above the guard.

## CLAUDE.md

The **"Reference ONUs"** section still says *"PLACING IS THE CLAIM — there is no
power column and nothing detects one"* and *"Never soften that copy… or reduce
the dialog to a one-click toggle."* That rule has been deliberately **replaced**,
and the replacement needs writing down with its reason:

> Placing was the claim while a dozen pins existed. It broke the moment a fleet
> surveyed its drops — `place != null` made every located customer a reference
> point, and one morning produced 30 accidental witnesses. The claim is now an
> explicit toggle with its own route; recording a location is the same act from
> the desk as from the handset. The CONTRACT COPY survives unchanged and still
> may not be softened — it moved from the dialog's header onto the toggle it
> guards.

Also worth recording: **the fix that was rejected as too clever.** Making
`witness` an explicit-but-optional key on the location route worked and still let
the next caller reintroduce the bug; taking the key out of the payload entirely
is what makes it unsayable. Same instinct as `clean_field_onu_payload` having no
`witness` key.
