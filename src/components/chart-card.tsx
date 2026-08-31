import type { ReactNode } from "react"

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/coss/components/card"

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
    <Card className={className}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm">{title}</CardTitle>
          {actions}
        </div>
        {description ? (
          <p className="text-xs text-muted-foreground">{description}</p>
        ) : null}
        <div className="flex items-center justify-between gap-2">
          {total !== undefined ? (
            <div className="flex items-baseline gap-1.5">
              {totalLabel ? (
                <span className="text-xs text-muted-foreground">
                  {totalLabel}
                </span>
              ) : null}
              <span className="text-2xl font-semibold tabular-nums">
                {total}
              </span>
            </div>
          ) : (
            <span />
          )}
          {legend}
        </div>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="flex h-56 items-center justify-center text-sm text-muted-foreground">
            加载中…
          </div>
        ) : (
          children
        )}
      </CardContent>
    </Card>
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
        <span key={item.label} className="flex items-center gap-1 text-xs">
          <span
            className="size-2 rounded-full"
            style={{ backgroundColor: item.color }}
          />
          <span className="text-muted-foreground">{item.label}</span>
        </span>
      ))}
    </div>
  )
}
