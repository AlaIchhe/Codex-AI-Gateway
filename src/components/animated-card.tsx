import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

/** 通用内容面板。名称与 `delay` 为兼容旧调用方保留，不再有动画。 */
export function AnimatedCard({
  title,
  description,
  children,
  contentClassName,
  className,
}: {
  title: string
  description?: string
  children: ReactNode
  delay?: number
  contentClassName?: string
  className?: string
}) {
  return (
    <section className={cn("rounded-lg border bg-card", className)}>
      <header className="space-y-0.5 border-b px-4 py-3">
        <h2 className="text-sm font-medium">{title}</h2>
        {description ? (
          <p className="text-xs text-muted-foreground">{description}</p>
        ) : null}
      </header>
      <div className={cn("p-4", contentClassName)}>{children}</div>
    </section>
  )
}
