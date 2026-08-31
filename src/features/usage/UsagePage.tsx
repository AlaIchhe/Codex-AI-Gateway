import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query"
import { useEffect, useMemo, useRef, useState } from "react"

import { ChartCard, ChartLegend } from "@/components/chart-card"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Tooltip,
  XAxis,
  YAxis,
} from "@/components/charts/barrel"
import { UsageBreakdownChart } from "@/components/charts/UsageBreakdownChart"
import { UsageChart } from "@/components/charts/UsageChart"
import { Button } from "@/components/coss/components/button"
import { Input } from "@/components/coss/components/input"
import { Label } from "@/components/coss/components/label"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/coss/components/table"
import { Overlay } from "@/components/coss/overlay"
import { BentoGrid } from "@/components/magicui/bento-grid"
import { BlurFade } from "@/components/magicui/blur-fade"
import { PageHeader } from "@/components/page-header"
import { StatCard } from "@/components/stat-card"
import { api, type UsageAttempt } from "@/lib/api"
import { useOverlaySearch } from "@/lib/search-params"

function compact(n: number): string {
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

export function UsagePage() {
  const [retentionOpen, setRetentionOpen] = useOverlaySearch("retention")
  const [retention, setRetention] = useState("30")
  const [keyword, setKeyword] = useState("")
  const [outcome, setOutcome] = useState("all")
  const [basis, setBasis] = useState("all")

  const periodData = useQuery({
    queryKey: ["usage-summary", "period"],
    queryFn: () => api.listUsageSummary("period"),
  })
  const modelData = useQuery({
    queryKey: ["usage-summary", "canonical_model"],
    queryFn: () => api.listUsageSummary("canonical_model"),
  })
  const upstreamData = useQuery({
    queryKey: ["usage-summary", "upstream"],
    queryFn: () => api.listUsageSummary("upstream"),
  })
  const attempts = useInfiniteQuery({
    queryKey: ["usage-attempts", outcome, basis],
    queryFn: ({ pageParam }) =>
      api.listUsageAttempts({ limit: 50, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
  const loadMoreRef = useRef<HTMLTableRowElement | null>(null)
  const allAttempts = useMemo(
    () => attempts.data?.pages.flatMap((page) => page.items) ?? [],
    [attempts.data],
  )
  useEffect(() => {
    const node = loadMoreRef.current
    if (!node) return
    const observer = new IntersectionObserver((entries) => {
      if (
        entries[0]?.isIntersecting &&
        attempts.hasNextPage &&
        !attempts.isFetchingNextPage
      ) {
        void attempts.fetchNextPage()
      }
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [
    attempts.hasNextPage,
    attempts.isFetchingNextPage,
    attempts.fetchNextPage,
  ])
  const save = useMutation({
    mutationFn: () => api.updateRetention(Number(retention)),
    onSuccess: () => setRetentionOpen(null),
  })

  const periodRows = periodData.data?.rows ?? []
  const modelRows = modelData.data?.rows ?? []
  const upstreamRows = upstreamData.data?.rows ?? []

  const totals = useMemo(
    () =>
      periodRows.reduce(
        (acc, row) => ({
          attempts: acc.attempts + row.attempts,
          inputTokens: acc.inputTokens + row.provider_reported_input_tokens,
          outputTokens: acc.outputTokens + row.estimated_output_tokens,
          reasoningTokens: acc.reasoningTokens + row.reasoning_tokens,
          cacheReadTokens: acc.cacheReadTokens + row.cache_read_tokens,
          costMinorUnits: acc.costMinorUnits + row.cost_minor_units,
        }),
        {
          attempts: 0,
          inputTokens: 0,
          outputTokens: 0,
          reasoningTokens: 0,
          cacheReadTokens: 0,
          costMinorUnits: 0,
        },
      ),
    [periodRows],
  )
  const totalTokens =
    totals.inputTokens + totals.outputTokens + totals.reasoningTokens
  const activeModels = modelRows.filter((r) => r.attempts > 0).length
  const activeUpstreams = upstreamRows.filter((r) => r.attempts > 0).length

  const filteredAttempts = allAttempts.filter((attempt) => {
    const haystack = [
      attempt.canonical_model_label,
      attempt.upstream_label,
      attempt.outbound_protocol,
      attempt.error_mapping_code,
      attempt.fallback_trigger,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
    if (keyword && !haystack.includes(keyword.toLowerCase())) return false
    if (outcome !== "all" && attempt.outcome !== outcome) return false
    if (basis !== "all" && attempt.reporting_basis !== basis) return false
    return true
  })

  function exportCsv() {
    const headers = [
      "时间",
      "模型",
      "上游",
      "协议",
      "尝试",
      "触发",
      "结果",
      "计费依据",
      "错误映射",
    ]
    const values = filteredAttempts.map((attempt: UsageAttempt) => [
      attempt.started_at,
      attempt.canonical_model_label,
      attempt.upstream_label,
      attempt.outbound_protocol,
      attempt.attempt_ordinal,
      attempt.fallback_trigger,
      attempt.outcome,
      attempt.reporting_basis,
      attempt.error_mapping_code,
    ])
    const csvEscape = (value: unknown) =>
      `"${String(value ?? "").replaceAll('"', '""')}"`
    const csv = [headers, ...values]
      .map((row) => row.map(csvEscape).join(","))
      .join("\n")
    const url = URL.createObjectURL(
      new Blob([csv], { type: "text/csv;charset=utf-8" }),
    )
    const link = document.createElement("a")
    link.href = url
    link.download = "usage-attempts.csv"
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <section aria-labelledby="usage-heading" className="space-y-4">
      <PageHeader
        title="用量"
        description="请求量、token、成本与上游归因的实时聚合。"
        actions={
          <Button size="sm" variant="outline" onClick={exportCsv}>
            导出 CSV
          </Button>
        }
      />

      {/* Stat cards */}
      <BentoGrid className="grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="总请求"
          value={totals.attempts}
          description="周期内全部尝试"
        />
        <StatCard
          label="总 token"
          value={totalTokens}
          description={`输入 ${compact(totals.inputTokens)} · 输出 ${compact(totals.outputTokens)}`}
          delay={0.04}
        />
        <StatCard
          label="总成本"
          value={totals.costMinorUnits}
          description="最小货币单位"
          delay={0.08}
        />
        <StatCard
          label="活跃模型"
          value={activeModels}
          description={`${activeUpstreams} 个上游`}
          delay={0.12}
        />
      </BentoGrid>

      {/* Chart grid */}
      <div className="grid gap-4 lg:grid-cols-2">
        <BlurFade delay={0}>
          <ChartCard
            title="请求量"
            loading={periodData.isLoading}
            total={totals.attempts}
            totalLabel="Total"
            legend={
              <ChartLegend
                items={[{ label: "尝试次数", color: "var(--chart-1)" }]}
              />
            }
          >
            <UsageChart rows={periodRows} />
          </ChartCard>
        </BlurFade>

        <BlurFade delay={0.04}>
          <ChartCard
            title="Token Usage"
            loading={periodData.isLoading}
            total={totalTokens}
            totalLabel="Total"
            legend={
              <ChartLegend
                items={[
                  { label: "输入", color: "var(--chart-1)" },
                  { label: "输出", color: "var(--chart-2)" },
                  { label: "推理", color: "var(--chart-3)" },
                  { label: "缓存", color: "var(--chart-4)" },
                ]}
              />
            }
          >
            <UsageBreakdownChart rows={periodRows} />
          </ChartCard>
        </BlurFade>

        <BlurFade delay={0.08}>
          <ChartCard
            title="模型请求分布"
            loading={modelData.isLoading}
            total={totals.attempts}
            totalLabel="Total"
          >
            <div className="h-72 w-full">
              <BarChart
                data={modelRows.slice(0, 8)}
                layout="vertical"
                margin={{ left: 8, right: 8, top: 8, bottom: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                <XAxis type="number" stroke="currentColor" fontSize={12} />
                <YAxis
                  type="category"
                  dataKey="bucket_start"
                  width={140}
                  stroke="currentColor"
                  fontSize={12}
                />
                <Tooltip />
                <Bar
                  dataKey="attempts"
                  name="请求"
                  fill="var(--chart-2)"
                  radius={[0, 4, 4, 0]}
                />
              </BarChart>
            </div>
          </ChartCard>
        </BlurFade>

        <BlurFade delay={0.12}>
          <ChartCard
            title="上游请求分布"
            loading={upstreamData.isLoading}
            total={totals.attempts}
            totalLabel="Total"
          >
            <div className="h-72 w-full">
              <BarChart
                data={upstreamRows.slice(0, 8)}
                layout="vertical"
                margin={{ left: 8, right: 8, top: 8, bottom: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                <XAxis type="number" stroke="currentColor" fontSize={12} />
                <YAxis
                  type="category"
                  dataKey="bucket_start"
                  width={140}
                  stroke="currentColor"
                  fontSize={12}
                />
                <Tooltip />
                <Bar
                  dataKey="attempts"
                  name="请求"
                  fill="var(--chart-3)"
                  radius={[0, 4, 4, 0]}
                />
              </BarChart>
            </div>
          </ChartCard>
        </BlurFade>
      </div>

      {/* Attempts audit */}
      <BlurFade delay={0.16}>
        <ChartCard
          title="Attempts 审计"
          description="按关键词、结果与计费依据筛选。"
        >
          <div className="mb-4 grid gap-3 md:grid-cols-4">
            <div className="grid gap-2">
              <Label htmlFor="attempt-keyword">关键词</Label>
              <Input
                id="attempt-keyword"
                value={keyword}
                onChange={(event) => setKeyword(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="attempt-outcome">结果</Label>
              <select
                id="attempt-outcome"
                className="min-h-11 border bg-background px-3 py-2 text-sm"
                value={outcome}
                onChange={(event) => setOutcome(event.target.value)}
              >
                <option value="all">全部</option>
                <option value="completed">完成</option>
                <option value="failed">失败</option>
                <option value="cancelled">取消</option>
                <option value="timed_out">超时</option>
                <option value="interrupted">中断</option>
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="attempt-basis">计费依据</Label>
              <select
                id="attempt-basis"
                className="min-h-11 border bg-background px-3 py-2 text-sm"
                value={basis}
                onChange={(event) => setBasis(event.target.value)}
              >
                <option value="all">全部</option>
                <option value="provider_reported">provider 上报</option>
                <option value="estimated">本地估算</option>
                <option value="mixed">混合</option>
              </select>
            </div>
            <div className="flex items-end">
              <Button size="sm" variant="outline" onClick={exportCsv}>
                导出 CSV
              </Button>
            </div>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>上游</TableHead>
                <TableHead>尝试</TableHead>
                <TableHead>结果</TableHead>
                <TableHead>计费依据</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredAttempts.map((attempt) => (
                <TableRow key={attempt.id}>
                  <TableCell>{attempt.started_at}</TableCell>
                  <TableCell>{attempt.canonical_model_label ?? "-"}</TableCell>
                  <TableCell>{attempt.upstream_label ?? "-"}</TableCell>
                  <TableCell>
                    {attempt.attempt_ordinal ?? 1}
                    {attempt.fallback_trigger
                      ? `（${attempt.fallback_trigger}）`
                      : ""}
                  </TableCell>
                  <TableCell>{attempt.outcome}</TableCell>
                  <TableCell>
                    {attempt.reporting_basis === "provider_reported"
                      ? "provider 上报"
                      : attempt.reporting_basis === "mixed"
                        ? "混合"
                        : "本地估算"}
                  </TableCell>
                </TableRow>
              ))}
              {!filteredAttempts.length && !attempts.isLoading && (
                <TableRow>
                  <TableCell
                    colSpan={6}
                    className="text-center text-muted-foreground"
                  >
                    暂无匹配的用量记录。
                  </TableCell>
                </TableRow>
              )}
              {attempts.hasNextPage && (
                <TableRow ref={loadMoreRef}>
                  <TableCell
                    colSpan={6}
                    className="text-center text-muted-foreground"
                  >
                    {attempts.isFetchingNextPage
                      ? "加载中…"
                      : "滚动到底部加载更多"}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </ChartCard>
      </BlurFade>

      <Overlay
        open={Boolean(retentionOpen)}
        onClose={() => setRetentionOpen(null)}
        title="用量保留期"
        variant="sheet"
      >
        <div className="space-y-4">
          <Label htmlFor="retention-days">保留天数</Label>
          <Input
            id="retention-days"
            type="number"
            min={1}
            value={retention}
            onChange={(event) => setRetention(event.target.value)}
          />
          <Button onClick={() => save.mutate()}>保存</Button>
        </div>
      </Overlay>
    </section>
  )
}
