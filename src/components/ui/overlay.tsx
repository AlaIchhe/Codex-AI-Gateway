import { X } from "lucide-react"
import { type ReactNode, useEffect, useRef } from "react"
import { createPortal } from "react-dom"
import { Button } from "./button"

type OverlayProps = {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  variant?: "dialog" | "sheet"
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element.offsetParent !== null)
}

export function Overlay({
  open,
  onClose,
  title,
  children,
  variant = "dialog",
}: OverlayProps) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    const appRoot = document.getElementById("root")
    const overlayRoot = document.createElement("div")
    overlayRoot.setAttribute("data-overlay-portal", "true")
    document.body.appendChild(overlayRoot)
    if (appRoot) appRoot.setAttribute("inert", "true")
    const next = focusableElements(ref.current ?? document.body)[0]
    ;(next ?? ref.current)?.focus()

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        onClose()
      }
      if (event.key !== "Tab") return
      const items = focusableElements(ref.current ?? document.body)
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("keydown", onKey)
      overlayRoot.remove()
      if (appRoot) appRoot.removeAttribute("inert")
      previous?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null
  const shell =
    variant === "sheet"
      ? "fixed inset-y-0 right-0 z-50 w-full max-w-xl overflow-y-auto border-l bg-background p-6 shadow-xl"
      : "fixed left-1/2 top-1/2 z-50 w-[min(640px,92vw)] max-h-[86vh] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border bg-background p-6 shadow-xl"

  return createPortal(
    <div
      className="fixed inset-0 z-40 bg-black/40"
      data-testid={`${variant}-overlay`}
    >
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={shell}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-4">
          <h2 className="text-lg font-semibold">{title}</h2>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label="关闭"
            onClick={onClose}
            className="min-h-11 min-w-11"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  )
}
