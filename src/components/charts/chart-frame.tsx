import { lazy, type ReactNode, Suspense } from "react"

const RechartsResponsive = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.ResponsiveContainer }
})

export function ChartFrame({
  testId,
  label,
  children,
}: {
  testId: string
  label: string
  children: ReactNode
}) {
  return (
    <div
      className="h-72 w-full"
      data-testid={testId}
      role="img"
      aria-label={label}
    >
      <Suspense
        fallback={
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            图表加载中…
          </div>
        }
      >
        <RechartsResponsive width="100%" height="100%">
          {children}
        </RechartsResponsive>
      </Suspense>
    </div>
  )
}
