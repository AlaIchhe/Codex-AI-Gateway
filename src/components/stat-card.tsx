import type { ReactNode } from "react"

import { BlurFade } from "@/components/magicui/blur-fade"
import { MagicCard } from "@/components/magicui/magic-card"
import { NumberTicker } from "@/components/magicui/number-ticker"

export function StatCard({
  label,
  value,
  description,
  icon,
  delay = 0,
  decimalPlaces = 0,
  className,
}: {
  label: string
  value: number
  description?: string
  icon?: ReactNode
  delay?: number
  decimalPlaces?: number
  className?: string
}) {
  return (
    <BlurFade className={className} delay={delay}>
      <MagicCard
        className="h-full rounded-xl border bg-card p-4 shadow-sm"
        gradientOpacity={0.08}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-muted-foreground">{label}</p>
            <p className="mt-2 text-2xl font-semibold tracking-tight">
              <NumberTicker
                value={value}
                decimalPlaces={decimalPlaces}
                className="text-foreground"
              />
            </p>
            {description ? (
              <p className="mt-1 text-xs text-muted-foreground">
                {description}
              </p>
            ) : null}
          </div>
          {icon ? (
            <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-muted text-foreground">
              {icon}
            </span>
          ) : null}
        </div>
      </MagicCard>
    </BlurFade>
  )
}
