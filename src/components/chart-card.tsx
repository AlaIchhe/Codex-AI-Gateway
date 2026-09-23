import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

export function ChartCard({
  title,
  description,
  total,
  totalLabel,
  legend,
  actions,
  loading,
  children,
  className,
}: {
  title: string
  description?: string
  total?: ReactNode
  totalLabel?: string
  legend?: ReactNode
  actions?: ReactNode
  loading?: boolean
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={cn("flex flex-col rounded-lg border bg-card", className)}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 px-4 pt-4">
        <div className="min-w-0 space-y-0.5">
          <h2 className="text-sm font-medium">{title}</h2>
          {description ? (
            <p className="text-xs text-muted-foreground">{description}</p>
          ) : null}
          {total !== undefined ? (
            <p className="flex items-baseline gap-1.5 pt-1.5">
              <span className="font-mono text-xl font-medium tracking-tight tabular-nums">
                {total}
              </span>
              {totalLabel ? (
                <span className="text-xs text-muted-foreground">
                  {totalLabel}
                </span>
              ) : null}
            </p>
          ) : null}
        </div>
        {actions || legend ? (
          <div className="flex items-center gap-3">
            {legend}
            {actions}
          </div>
        ) : null}
      </header>
      <div className="flex-1 p-4">
        {loading ? (
          <div className="grid h-56 place-items-center text-xs text-muted-foreground">
            加载中…
          </div>
        ) : (
          children
        )}
      </div>
    </section>
  )
}

export function ChartLegend({
  items,
}: {
  items: { label: string; color: string }[]
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      {items.map((item) => (
        <span key={item.label} className="flex items-center gap-1.5 text-xs">
          <span
            className="size-2 rounded-[2px]"
            style={{ backgroundColor: item.color }}
          />
          <span className="text-muted-foreground">{item.label}</span>
        </span>
      ))}
    </div>
  )
}
