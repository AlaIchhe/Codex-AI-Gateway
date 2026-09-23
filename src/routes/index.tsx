import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { ArrowUpRight } from "lucide-react"
import { useMemo } from "react"

import { ChartCard } from "@/components/chart-card"
import {
  Area,
  AreaChart,
  CartesianGrid,
  XAxis,
  YAxis,
} from "@/components/charts/barrel"
import { PageHeader } from "@/components/page-header"
import { StatCard } from "@/components/stat-card"
import {
  type ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart"
import { api } from "@/lib/api"

function compact(n: number): string {
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

const chartConfig = {
  attempts: { label: "请求", color: "var(--chart-1)" },
} satisfies ChartConfig

const shortcuts = [
  { to: "/upstreams", title: "上游", description: "接入、探测与排序管理" },
  { to: "/models", title: "模型", description: "聚合目录与路由优先级" },
  { to: "/usage", title: "用量", description: "趋势、归属与 attempt 审计" },
] as const

function DashboardPage() {
  const upstreams = useQuery({
    queryKey: ["upstreams"],
    queryFn: api.listUpstreams,
  })
  const models = useQuery({ queryKey: ["models"], queryFn: api.listModels })
  const usage = useQuery({
    queryKey: ["usage-summary", "period"],
    queryFn: () => api.listUsageSummary("period"),
  })

  const periodRows = usage.data?.rows ?? []
  const totals = useMemo(
    () =>
      periodRows.reduce(
        (acc, row) => ({
          attempts: acc.attempts + row.attempts,
          inputTokens: acc.inputTokens + row.provider_reported_input_tokens,
          outputTokens: acc.outputTokens + row.estimated_output_tokens,
          costMinorUnits: acc.costMinorUnits + row.cost_minor_units,
        }),
        { attempts: 0, inputTokens: 0, outputTokens: 0, costMinorUnits: 0 },
      ),
    [periodRows],
  )

  const enabledUpstreams = (upstreams.data ?? []).filter(
    (item) => item.status === "enabled",
  ).length
  const latestAttempts = periodRows.slice(-4).reverse()

  return (
    <section aria-labelledby="dashboard-heading" className="space-y-6">
      <PageHeader
        title="总览"
        description="汇聚上游健康、模型目录与用量趋势，快速发现路由异常。"
      />

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <StatCard
          label="可用上游"
          value={enabledUpstreams}
          description={`共 ${(upstreams.data ?? []).length} 个上游`}
        />
        <StatCard
          label="规范模型"
          value={(models.data ?? []).length}
          description="聚合后的可路由目录"
        />
        <StatCard
          label="总请求"
          value={totals.attempts}
          description="周期汇总累计"
        />
        <StatCard
          label="总成本"
          value={totals.costMinorUnits}
          description="provider 上报优先"
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-[2fr_1fr]">
        <ChartCard
          title="请求量"
          loading={usage.isLoading}
          total={compact(totals.attempts)}
          totalLabel="次请求"
        >
          <ChartContainer config={chartConfig} className="h-64 w-full">
            <AreaChart
              data={periodRows}
              margin={{ left: 0, right: 4, top: 8, bottom: 0 }}
            >
              <CartesianGrid vertical={false} />
              <XAxis
                dataKey="bucket_start"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                tickMargin={8}
              />
              <YAxis
                fontSize={11}
                tickLine={false}
                axisLine={false}
                width={36}
              />
              <ChartTooltip content={<ChartTooltipContent />} />
              <Area
                type="monotone"
                dataKey="attempts"
                name="请求"
                stroke="var(--color-attempts)"
                strokeWidth={1.5}
                fill="var(--color-attempts)"
                fillOpacity={0.08}
              />
            </AreaChart>
          </ChartContainer>
        </ChartCard>

        <ChartCard title="最近周期" description="最近的周期汇总记录">
          {latestAttempts.length ? (
            <ul className="-mx-4 divide-y border-y text-sm">
              {latestAttempts.map((row) => (
                <li
                  key={row.bucket_start}
                  className="flex items-center justify-between gap-3 px-4 py-2.5"
                >
                  <span className="font-mono text-xs">{row.bucket_start}</span>
                  <span className="font-mono text-xs text-muted-foreground tabular-nums">
                    {row.attempts} 次 ·{" "}
                    {compact(
                      row.provider_reported_input_tokens +
                        row.estimated_output_tokens,
                    )}{" "}
                    tok
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">暂无用量汇总。</p>
          )}
        </ChartCard>
      </div>

      <nav
        aria-label="快速入口"
        className="grid divide-y rounded-lg border bg-card md:grid-cols-3 md:divide-x md:divide-y-0"
      >
        {shortcuts.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className="group flex items-center justify-between gap-3 px-4 py-3.5 transition-colors first:rounded-t-lg last:rounded-b-lg hover:bg-accent/60 md:first:rounded-l-lg md:first:rounded-tr-none md:last:rounded-r-lg md:last:rounded-bl-none"
          >
            <span className="min-w-0">
              <span className="block text-sm font-medium">{item.title}</span>
              <span className="block truncate text-xs text-muted-foreground">
                {item.description}
              </span>
            </span>
            <ArrowUpRight className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5 group-hover:text-foreground" />
          </Link>
        ))}
      </nav>
    </section>
  )
}

export const Route = createFileRoute("/")({
  component: DashboardPage,
})
