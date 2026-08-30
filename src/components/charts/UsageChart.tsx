import { lazy, Suspense } from "react"

const RechartsResponsive = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.ResponsiveContainer }
})
const AreaChart = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.AreaChart }
})
const Area = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Area }
})
const XAxis = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.XAxis }
})
const YAxis = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.YAxis }
})
const Tooltip = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Tooltip }
})

export type ChartRow = {
  bucket_start: string
  attempts: number
  provider_reported_input_tokens: number
  estimated_output_tokens: number
}

export function UsageChart({ rows }: { rows: ChartRow[] }) {
  return (
    <div
      className="h-72 w-full"
      data-testid="usage-chart"
      role="img"
      aria-label="周期用量趋势图"
    >
      <Suspense
        fallback={
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            图表加载中…
          </div>
        }
      >
        <RechartsResponsive width="100%" height="100%">
          <AreaChart
            data={rows}
            margin={{ left: 8, right: 8, top: 8, bottom: 8 }}
          >
            <XAxis dataKey="bucket_start" stroke="currentColor" fontSize={12} />
            <YAxis stroke="currentColor" fontSize={12} />
            <Tooltip />
            <Area
              type="monotone"
              dataKey="provider_reported_input_tokens"
              name="上报输入"
              stroke="var(--chart-1)"
              fill="var(--chart-1)"
            />
            <Area
              type="monotone"
              dataKey="estimated_output_tokens"
              name="估算输出"
              stroke="var(--chart-2)"
              fill="var(--chart-2)"
            />
          </AreaChart>
        </RechartsResponsive>
      </Suspense>
    </div>
  )
}
