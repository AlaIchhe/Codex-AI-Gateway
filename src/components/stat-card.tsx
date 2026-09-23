import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

const formatter = new Intl.NumberFormat("zh-CN")

/** `delay` 保留以兼容旧调用方；静态呈现，不再做入场动画。 */
export function StatCard({
  label,
  value,
  description,
  icon,
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
  const display =
    decimalPlaces > 0
      ? value.toLocaleString("zh-CN", {
          minimumFractionDigits: decimalPlaces,
          maximumFractionDigits: decimalPlaces,
        })
      : formatter.format(value)
  return (
    <div className={cn("rounded-lg border bg-card px-4 py-3.5", className)}>
      <div className="flex items-center justify-between gap-2 text-muted-foreground">
        <span className="text-xs font-medium">{label}</span>
        {icon ? (
          <span className="[&_svg]:size-3.5 [&_svg]:opacity-70">{icon}</span>
        ) : null}
      </div>
      <p className="mt-2 font-mono text-2xl font-medium tracking-tight tabular-nums">
        {display}
      </p>
      {description ? (
        <p className="mt-1 truncate text-xs text-muted-foreground">
          {description}
        </p>
      ) : null}
    </div>
  )
}
