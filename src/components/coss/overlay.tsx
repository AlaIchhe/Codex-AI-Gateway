import type { ReactNode } from "react"
import {
  Dialog,
  DialogHeader,
  DialogPanel,
  DialogPopup,
  DialogTitle,
} from "@/components/coss/components/dialog"
import {
  Sheet,
  SheetHeader,
  SheetPanel,
  SheetPopup,
  SheetTitle,
} from "@/components/coss/components/sheet"

export type OverlayProps = {
  open: boolean
  onClose: () => void
  title: string
  variant?: "dialog" | "sheet"
  children: ReactNode
}

/** 旧 Overlay API 的兼容封装，底层为 COSS Dialog/Sheet（Base UI）。 */
export function Overlay({
  open,
  onClose,
  title,
  variant = "dialog",
  children,
}: OverlayProps) {
  const handleOpenChange = (next: boolean) => {
    if (!next) onClose()
  }
  if (variant === "sheet") {
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetPopup className="w-full max-w-xl" data-testid="sheet-overlay">
          <SheetHeader>
            <SheetTitle>{title}</SheetTitle>
          </SheetHeader>
          <SheetPanel>{children}</SheetPanel>
        </SheetPopup>
      </Sheet>
    )
  }
  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPopup className="w-[min(640px,92vw)]" data-testid="dialog-overlay">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <DialogPanel>{children}</DialogPanel>
      </DialogPopup>
    </Dialog>
  )
}
