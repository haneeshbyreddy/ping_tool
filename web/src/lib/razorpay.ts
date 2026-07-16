import type { BillingOrder } from "./api"

/** Razorpay Checkout glue: load the hosted script once, open the modal, hand
 * back the signed result for central to verify (`/api/billing/verify`). The
 * script is Razorpay-hosted by design — like the Google map tiles, the
 * browser talks to the gateway, central never does (beyond the order POST). */

declare global {
  interface Window { Razorpay?: new (options: unknown) => { open: () => void } }
}

export interface CheckoutResult {
  razorpay_order_id: string
  razorpay_payment_id: string
  razorpay_signature: string
}

let loader: Promise<boolean> | null = null

function loadScript(): Promise<boolean> {
  if (window.Razorpay) return Promise.resolve(true)
  if (!loader) {
    loader = new Promise((resolve) => {
      const s = document.createElement("script")
      s.src = "https://checkout.razorpay.com/v1/checkout.js"
      s.onload = () => resolve(true)
      // allow a retry on the next click instead of caching the failure
      s.onerror = () => { loader = null; resolve(false) }
      document.head.appendChild(s)
    })
  }
  return loader
}

/** Resolves with the signed payment on success, null when the user closes
 * the modal without paying. Rejects only when the script itself won't load. */
export async function openCheckout(order: BillingOrder): Promise<CheckoutResult | null> {
  const ok = await loadScript()
  if (!ok || !window.Razorpay) {
    throw new Error("Couldn't load Razorpay checkout — check your connection and try again")
  }
  return new Promise((resolve) => {
    new window.Razorpay!({
      key: order.key_id,
      order_id: order.order_id,
      amount: order.amount,
      currency: order.currency,
      name: "WISP Central",
      description: order.description,
      // desaturated near-black, matching the dashboard chrome
      theme: { color: "#27272a" },
      modal: { ondismiss: () => resolve(null) },
      handler: (resp: CheckoutResult) => resolve(resp),
    }).open()
  })
}
