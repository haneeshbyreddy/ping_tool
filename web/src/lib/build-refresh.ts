// The SPA reloads itself when central starts serving a newer build. The build
// id is the entry chunk's hash: the server reads it off index.html on disk and
// pushes it down the SSE stream (`event: build`); we read our own off the
// <script> tag this page booted from, so both sides parse the same fact and
// there is no second versioning scheme to keep in step.
//
// Reload policy is "when safe", never mid-work: immediately while the tab is
// hidden, otherwise on the next navigation or the next time the tab is hidden.
// A dialog being open blocks the hidden-reload (alt-tabbing away mid-edit must
// not discard the edit).

const ENTRY_RE = /\/assets\/index-([\w-]+)\.js/
const RELOADED_KEY = "wisp:reloaded-for"
const CHUNK_RELOAD_KEY = "wisp:chunk-reload"

export function ownBuildId(): string | null {
  for (const s of document.querySelectorAll<HTMLScriptElement>("script[src]")) {
    const m = ENTRY_RE.exec(s.src)
    if (m) return m[1]
  }
  return null // dev server — the watcher stays disarmed
}

let pending: string | null = null
let armed = false

function dialogOpen(): boolean {
  return !!document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]')
}

function reloadNow() {
  if (!pending) return
  sessionStorage.setItem(RELOADED_KEY, pending)
  window.location.reload()
}

function onNavigate() {
  reloadNow()
}

function onHidden() {
  if (document.visibilityState === "hidden" && !dialogOpen()) reloadNow()
}

export function onServerBuild(id: string) {
  const own = ownBuildId()
  if (!own || !id || id === own) return
  // A reload that didn't change our build (cached index, parse drift) must not
  // loop — one automatic attempt per build id, then leave it to a manual reload.
  if (sessionStorage.getItem(RELOADED_KEY) === id) return
  pending = id
  if (!armed) {
    armed = true
    window.addEventListener("hashchange", onNavigate)
    document.addEventListener("visibilitychange", onHidden)
  }
  if (document.visibilityState === "hidden" && !dialogOpen()) reloadNow()
}

// A deploy replaces the hashed chunk files, so a tab from before it can 404 on
// a lazy route's chunk. Vite reports that as `vite:preloadError`; one automatic
// reload picks up the new index.html. Keyed by route so a genuinely broken
// chunk can't reload-loop.
export function installChunkGuard() {
  window.addEventListener("vite:preloadError", (e) => {
    if (sessionStorage.getItem(CHUNK_RELOAD_KEY) === window.location.hash) return
    e.preventDefault()
    sessionStorage.setItem(CHUNK_RELOAD_KEY, window.location.hash)
    window.location.reload()
  })
}
