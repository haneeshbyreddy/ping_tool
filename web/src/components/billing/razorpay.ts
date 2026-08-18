// The gateway's checkout bundle, loaded ON DEMAND and ON THIS PAGE ONLY.
//
// It is deliberately NOT in index.html: the SPA is the whole product and every
// other route would then be paying for a third-party script it never uses, on
// every load, for a page most owners open once a month. It is also a
// third-party origin — the fewer routes that talk to it, the smaller the
// surface.
//
// One injection per session, and the promise is the lock: two clicks on Pay
// before the first load settles must not append a second <script>, because the
// checkout bundle does not survive being evaluated twice.

const SRC = "https://checkout.razorpay.com/v1/checkout.js"

export interface RazorpayReturn {
  razorpay_order_id: string
  razorpay_payment_id: string
  razorpay_signature: string
}

/** The gateway-neutral name for the same payload. POST it to
 *  /api/billing/return as-is: the field names ARE the wire contract, so
 *  anything that renames them here has to rename them there too. */
export type CheckoutReturn = RazorpayReturn

export interface RazorpayFailure {
  error?: {
    code?: string
    description?: string
    reason?: string
    step?: string
  }
}

export interface RazorpayOptions {
  key: string
  amount: number
  currency: string
  order_id: string
  name?: string
  description?: string
  prefill?: { contact?: string; email?: string }
  handler?: (r: RazorpayReturn) => void
  modal?: { ondismiss?: () => void }
  theme?: { color?: string }
  retry?: { enabled: boolean }
}

export interface RazorpayInstance {
  open(): void
  // Optional: the bundle has shipped builds without it, and a hard call on a
  // missing method would take down the pay button over a nicety.
  on?: (event: "payment.failed", cb: (e: RazorpayFailure) => void) => void
}

type RazorpayCtor = new (options: RazorpayOptions) => RazorpayInstance

declare global {
  interface Window {
    Razorpay?: RazorpayCtor
  }
}

let pending: Promise<RazorpayCtor> | null = null

export function loadRazorpay(): Promise<RazorpayCtor> {
  if (window.Razorpay) return Promise.resolve(window.Razorpay)
  if (pending) return pending

  pending = new Promise<RazorpayCtor>((resolve, reject) => {
    const fail = (why: string) => {
      // Drop the cached promise so a retry can try the network again; a stuck
      // rejected promise would turn one bad moment into a dead button.
      pending = null
      reject(new Error(why))
    }
    const settle = () => {
      if (window.Razorpay) resolve(window.Razorpay)
      else fail("the payment window did not initialise")
    }

    // A tag that neither loads nor errors (a captive portal answering 200 with
    // its own page) would otherwise leave the pay button spinning forever.
    // Settling twice is a no-op, so this is safe after either outcome, and it
    // is armed BEFORE the branch below so a retry onto an already-present tag
    // is bounded too.
    window.setTimeout(() => {
      if (!window.Razorpay) fail("the payment window did not load in time")
    }, 20_000)

    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${SRC}"]`)
    if (existing) {
      if (existing.dataset.wispLoaded === "1") { settle(); return }
      existing.addEventListener("load", settle, { once: true })
      existing.addEventListener("error",
        () => fail("the payment window could not be reached"), { once: true })
      return
    }

    const el = document.createElement("script")
    el.src = SRC
    el.async = true
    el.addEventListener("load", () => {
      el.dataset.wispLoaded = "1"
      settle()
    }, { once: true })
    el.addEventListener("error", () => {
      el.remove()
      fail("the payment window could not be reached")
    }, { once: true })
    document.head.appendChild(el)
  })
  return pending
}

/** The checkout accepts a HEX brand colour and silently ignores anything else,
 *  so only hand it one when the live token actually is one. The theme panel
 *  can put any CSS colour in --primary. */
export function brandColor(): string | undefined {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--primary").trim()
  return /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i.test(raw) ? raw : undefined
}
