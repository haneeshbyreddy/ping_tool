import { useEffect, useState } from "react"
import { Maximize2, X } from "lucide-react"
import { cn } from "@/lib/utils"

export function QrImage({ src, className, imgClassName }: {
  src: string
  className?: string
  imgClassName?: string
}) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false) }
    window.addEventListener("keydown", onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = "hidden"
    return () => {
      window.removeEventListener("keydown", onKey)
      document.body.style.overflow = prev
    }
  }, [open])

  return (
    <>
      <button type="button" title="Tap to enlarge" onClick={() => setOpen(true)}
        className={cn("group relative inline-block cursor-zoom-in align-top", className)}>
        <img src={src} alt="Payment QR code" className={cn("block bg-white", imgClassName)} />
        <span className="pointer-events-none absolute right-1.5 bottom-1.5 rounded bg-black/55 p-1 text-white opacity-70 transition-opacity group-hover:opacity-100">
          <Maximize2 className="size-3.5" />
        </span>
      </button>
      {open && (
        <div role="dialog" aria-modal="true" aria-label="Payment QR code, enlarged"
          onClick={() => setOpen(false)}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm">
          <img src={src} alt="Payment QR code" onClick={(e) => e.stopPropagation()}
            className="max-h-[85vh] w-auto max-w-[90vw] rounded-2xl bg-white p-4 shadow-2xl" />
          <button type="button" aria-label="Close" onClick={() => setOpen(false)}
            className="absolute top-4 right-4 rounded-full bg-white/10 p-2 text-white transition-colors hover:bg-white/20">
            <X className="size-5" />
          </button>
        </div>
      )}
    </>
  )
}
