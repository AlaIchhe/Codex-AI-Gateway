import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import {
  ArrowRight,
  BarChart3,
  Coins,
  Cpu,
  Layers,
  Network,
  ShieldCheck,
} from "lucide-react"
import { useMemo } from "react"

import { ChartCard } from "@/components/chart-card"
import {
  Area,
  AreaChart,
  CartesianGrid,
  XAxis,
  YAxis,
} from "@/components/charts/barrel"
import { Badge } from "@/components/coss/components/badge"
import { AnimatedShinyText } from "@/components/magicui/animated-shiny-text"
import { BentoGrid } from "@/components/magicui/bento-grid"
import { BlurFade } from "@/components/magicui/blur-fade"
import { InteractiveHoverButton } from "@/components/magicui/interactive-hover-button"
import { MagicCard } from "@/components/magicui/magic-card"
import { OrbitingCircles } from "@/components/magicui/orbiting-circles"
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
    <section aria-labelledby="dashboard-heading" className="space-y-5">
      <BlurFade className="relative overflow-hidden border bg-card p-5">
        <div className="relative z-10 grid gap-6 lg:grid-cols-[1fr_auto] lg:items-center">
          <div>
            <Badge variant="secondary" className="mb-3">
              网关控制台
            </Badge>
            <h1
              id="dashboard-heading"
              className="text-3xl font-semibold tracking-tight"
            >
              全局网关运行总览
            </h1>
            <AnimatedShinyText className="mt-2 block max-w-none text-sm">
              汇聚上游健康、模型目录与用量趋势，快速发现路由异常。
            </AnimatedShinyText>
            <div className="mt-4 flex flex-wrap gap-2">
              <Link to="/upstreams">
                <InteractiveHoverButton type="button" className="h-9 text-sm">
                  管理上游
                </InteractiveHoverButton>
              </Link>
              <Link to="/usage">
                <InteractiveHoverButton type="button" className="h-9 text-sm">
                  查看用量
                </InteractiveHoverButton>
              </Link>
            </div>
          </div>

          <div className="relative hidden h-36 w-36 place-items-center lg:grid">
            <OrbitingCircles radius={58} duration={18} iconSize={26} reverse>
              <Network className="size-4 text-muted-foreground" />
              <Cpu className="size-4 text-muted-foreground" />
              <BarChart3 className="size-4 text-muted-foreground" />
            </OrbitingCircles>
            <ShieldCheck className="absolute size-7 text-primary" />
          </div>
        </div>
      </BlurFade>

      <BentoGrid className="grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="可用上游"
          value={enabledUpstreams}
          description={`共 ${(upstreams.data ?? []).length} 个上游`}
          icon={<Network className="size-4" />}
        />
        <StatCard
          label="规范模型"
          value={(models.data ?? []).length}
          description="聚合后的可路由目录"
          icon={<Cpu className="size-4" />}
          delay={0.04}
        />
        <StatCard
          label="总请求"
          value={totals.attempts}
          description="周期汇总累计"
          icon={<Layers className="size-4" />}
          delay={0.08}
        />
        <StatCard
          label="总成本"
          value={totals.costMinorUnits}
          description="provider 上报优先"
          icon={<Coins className="size-4" />}
          delay={0.12}
        />
      </BentoGrid>

      <div className="grid gap-4 lg:grid-cols-2">
        <BlurFade delay={0}>
          <ChartCard
            title="请求量"
            loading={usage.isLoading}
            total={totals.attempts}
            totalLabel="Total"
          >
            <ChartContainer
              config={
                {
                  attempts: { label: "请求", color: "var(--chart-1)" },
                } satisfies ChartConfig
              }
              className="h-72 w-full"
            >
              <AreaChart
                data={periodRows}
                margin={{ left: 8, right: 8, top: 8, bottom: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="bucket_start"
                  stroke="currentColor"
                  fontSize={12}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  stroke="currentColor"
                  fontSize={12}
                  tickLine={false}
                  axisLine={false}
                />
                <ChartTooltip content={<ChartTooltipContent />} />
                <Area
                  type="monotone"
                  dataKey="attempts"
                  name="请求"
                  stroke="var(--color-attempts)"
                  fill="var(--color-attempts)"
                  fillOpacity={0.15}
                />
              </AreaChart>
            </ChartContainer>
          </ChartCard>
        </BlurFade>

        <BlurFade delay={0.04}>
          <ChartCard title="最近周期" description="取最近的周期汇总记录。">
            <div className="space-y-3">
              {latestAttempts.map((row) => (
                <BlurFade
                  key={row.bucket_start}
                  className="flex items-center justify-between border bg-background px-3 py-2 text-sm"
                >
                  <span>{row.bucket_start}</span>
                  <span className="text-muted-foreground">
                    {row.attempts} 次尝试 ·{" "}
                    {compact(
                      row.provider_reported_input_tokens +
                        row.estimated_output_tokens,
                    )}{" "}
                    tok
                  </span>
                </BlurFade>
              ))}
              {!latestAttempts.length ? (
                <p className="text-sm text-muted-foreground">暂无用量汇总。</p>
              ) : null}
            </div>
          </ChartCard>
        </BlurFade>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        {[
          {
            to: "/upstreams" as const,
            title: "上游",
            description: "接入、探测与排序管理。",
            icon: Network,
          },
          {
            to: "/models" as const,
            title: "模型",
            description: "聚合目录与路由优先级。",
            icon: Cpu,
          },
          {
            to: "/usage" as const,
            title: "用量",
            description: "趋势、归属与 attempt 审计。",
            icon: BarChart3,
          },
        ].map((item, index) => {
          const Icon = item.icon
          return (
            <BlurFade key={item.to} delay={index * 0.04}>
              <MagicCard
                className="h-full border bg-card p-4"
                gradientOpacity={0.08}
              >
                <Link to={item.to} className="flex h-full flex-col gap-2">
                  <span className="grid size-9 place-items-center bg-muted">
                    <Icon className="size-4" />
                  </span>
                  <span className="text-base font-medium">{item.title}</span>
                  <span className="text-sm text-muted-foreground">
                    {item.description}
                  </span>
                  <span className="mt-auto flex items-center gap-1 text-sm text-primary">
                    进入
                    <ArrowRight className="size-3.5" />
                  </span>
                </Link>
              </MagicCard>
            </BlurFade>
          )
        })}
      </div>
    </section>
  )
}

const RoutePending = () => (
  <div className="flex min-h-[50vh] items-center justify-center">
    <div className="size-6 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
  </div>
)

export const Route = createFileRoute("/")({
  pendingComponent: RoutePending,
  component: DashboardPage,
})
