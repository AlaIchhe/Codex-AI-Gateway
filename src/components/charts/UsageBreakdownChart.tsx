import { lazy, Suspense } from "react"

const RechartsResponsive = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.ResponsiveContainer }
})
const BarChart = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.BarChart }
})
const Bar = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Bar }
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
const Legend = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Legend }
})

import type { UsageSummaryRow } from "@/lib/api"

function formatKey(value: string): string {
  if (value.length <= 18) return value
  return `${value.slice(0, 16)}…`
}

export function UsageBreakdownChart({ rows }: { rows: UsageSummaryRow[] }) {
  return (
    <div
      className="h-72 w-full"
      data-testid="usage-breakdown-chart"
      role="img"
      aria-label="用量结构与归属分布图"
    >
      <Suspense
        fallback={
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            图表加载中…
          </div>
        }
      >
        <RechartsResponsive width="100%" height="100%">
          <BarChart
            data={rows}
            margin={{ left: 8, right: 8, top: 8, bottom: 8 }}
          >
            <XAxis
              dataKey="bucket_start"
              tickFormatter={formatKey}
              stroke="currentColor"
              fontSize={12}
            />
            <YAxis stroke="currentColor" fontSize={12} />
            <Tooltip />
            <Legend />
            <Bar
              dataKey="provider_reported_input_tokens"
              name="provider 输入"
              stackId="tokens"
              fill="var(--chart-1)"
            />
            <Bar
              dataKey="estimated_output_tokens"
              name="估算输出"
              stackId="tokens"
              fill="var(--chart-2)"
            />
            <Bar
              dataKey="reasoning_tokens"
              name="推理 token"
              stackId="tokens"
              fill="var(--chart-3)"
            />
            <Bar
              dataKey="cache_read_tokens"
              name="缓存读取"
              stackId="tokens"
              fill="var(--chart-4)"
            />
            <Bar
              dataKey="cost_minor_units"
              name="成本（最小货币单位）"
              fill="var(--chart-5)"
            />
          </BarChart>
        </RechartsResponsive>
      </Suspense>
    </div>
  )
}
