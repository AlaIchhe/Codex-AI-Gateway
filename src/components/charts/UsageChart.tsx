import { Area, AreaChart, ChartFrame, Tooltip, XAxis, YAxis } from "./barrel"

export type ChartRow = {
  bucket_start: string
  attempts: number
  provider_reported_input_tokens: number
  estimated_output_tokens: number
}

export function UsageChart({ rows }: { rows: ChartRow[] }) {
  return (
    <ChartFrame testId="usage-chart" label="周期用量趋势图">
      <AreaChart data={rows} margin={{ left: 8, right: 8, top: 8, bottom: 8 }}>
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
    </ChartFrame>
  )
}
