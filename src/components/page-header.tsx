import type { ReactNode } from "react"

import { AnimatedShinyText } from "@/components/magicui/animated-shiny-text"
import { BlurFade } from "@/components/magicui/blur-fade"
import { cn } from "@/lib/utils"

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: string
  description?: string
  actions?: ReactNode
  className?: string
}) {
  return (
    <BlurFade
      className={cn(
        "flex flex-wrap items-end justify-between gap-3",
        className,
      )}
    >
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description ? (
          <AnimatedShinyText className="mt-1 block max-w-none text-sm">
            {description}
          </AnimatedShinyText>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </BlurFade>
  )
}
