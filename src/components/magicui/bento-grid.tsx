import type { ComponentPropsWithoutRef, ReactNode } from "react"
import { cn } from "@/lib/utils"

type BentoGridProps = ComponentPropsWithoutRef<"div"> & {
  children: ReactNode
}

export function BentoGrid({ children, className, ...props }: BentoGridProps) {
  return (
    <div
      className={cn(
        "grid w-full grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  )
}
