import { useMutation, useQuery } from "@tanstack/react-query"
import { useMemo, useState } from "react"

import { AnimatedCard } from "@/components/animated-card"
import { UsageBreakdownChart } from "@/components/charts/UsageBreakdownChart"
import { UsageChart } from "@/components/charts/UsageChart"
import { BentoGrid } from "@/components/magicui/bento-grid"
import { PageHeader } from "@/components/page-header"
import { StatCard } from "@/components/stat-card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Overlay } from "@/components/ui/overlay"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type UsageAttempt } from "@/lib/api"
import { useOverlaySearch } from "@/lib/search-params"

const groupOptions = [
  { value: "period", label: "周期" },
  { value: "canonical_model", label: "规范模型" },
  { value: "upstream", label: "上游" },
  { value: "offering", label: "Offering" },
  { value: "protocol", label: "协议" },
]

export function UsagePage() {
  const [retentionOpen, setRetentionOpen] = useOverlaySearch("retention")
  const [retention, setRetention] = useState("30")
  const [groupBy, setGroupBy] = useState("period")
  const [keyword, setKeyword] = useState("")
  const [outcome, setOutcome] = useState("all")
  const [basis, setBasis] = useState("all")
  const summary = useQuery({
    queryKey: ["usage-summary", groupBy],
    queryFn: () => api.listUsageSummary(groupBy),
  })
  const attempts = useQuery({
    queryKey: ["usage-attempts"],
    queryFn: api.listUsageAttempts,
  })
  const save = useMutation({
    mutationFn: () => api.updateRetention(Number(retention)),
    onSuccess: () => setRetentionOpen(null),
  })
  const rows = summary.data?.rows ?? []
  const usageTotals = useMemo(
    () =>
      rows.reduce(
        (acc, row) => ({
          attempts: acc.attempts + row.attempts,
          tokens:
            acc.tokens +
            row.provider_reported_input_tokens +
            row.estimated_output_tokens,
          costMinorUnits: acc.costMinorUnits + row.cost_minor_units,
        }),
        { attempts: 0, tokens: 0, costMinorUnits: 0 },
      ),
    [rows],
  )

  const filteredAttempts = (attempts.data ?? []).filter((attempt) => {
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
        description="图表优先展示趋势、结构与归属，保留精确 attempt 审计表格。"
        actions={
          <>
            <Button variant="outline" onClick={exportCsv}>
              导出 CSV
            </Button>
            <Button variant="outline" onClick={() => setRetentionOpen(true)}>
              保留期配置
            </Button>
          </>
        }
      />

      <BentoGrid className="md:grid-cols-3">
        <StatCard
          label="总尝试"
          value={usageTotals.attempts}
          description="当前筛选维度累计"
        />
        <StatCard
          label="token 累计"
          value={usageTotals.tokens}
          description="上报输入 + 估算输出"
          delay={0.06}
        />
        <StatCard
          label="成本（最小货币单位）"
          value={usageTotals.costMinorUnits}
          description="按 provider 上报优先"
          delay={0.12}
        />
      </BentoGrid>
      <AnimatedCard
        title="周期趋势"
        description="上报值与估算值在图表中分色显示。"
        beam
      >
        <UsageChart rows={rows} />
      </AnimatedCard>
      <AnimatedCard
        title="结构与归属分布"
        description="按所选维度查看 token、成本与尝试分布。"
        delay={0.06}
      >
        <div className="mb-4 grid gap-2 sm:max-w-xs">
          <Label htmlFor="usage-group">归属维度</Label>
          <select
            id="usage-group"
            className="min-h-11 rounded-md border bg-background px-3 py-2 text-sm"
            value={groupBy}
            onChange={(event) => setGroupBy(event.target.value)}
          >
            {groupOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <UsageBreakdownChart rows={rows} />
        <ul
          aria-label="图表汇总等价文本"
          className="mt-4 space-y-1 text-sm text-muted-foreground"
        >
          {rows.map((row) => (
            <li key={row.bucket_start}>
              {row.bucket_start}：尝试 {row.attempts}，provider 输入{" "}
              {row.provider_reported_input_tokens}，估算输出{" "}
              {row.estimated_output_tokens}，推理 {row.reasoning_tokens}，缓存{" "}
              {row.cache_read_tokens}，成本 {row.cost_minor_units}{" "}
              {row.currency}。
            </li>
          ))}
          {!rows.length && <li>暂无汇总数据。</li>}
        </ul>
      </AnimatedCard>
      <AnimatedCard
        title="attempts 审计"
        description="按关键词、结果与计费依据筛选，切换请求会显示多条尝试。"
        delay={0.12}
      >
        <div className="mb-4 grid gap-3 md:grid-cols-3">
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
              className="min-h-11 rounded-md border bg-background px-3 py-2 text-sm"
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
              className="min-h-11 rounded-md border bg-background px-3 py-2 text-sm"
              value={basis}
              onChange={(event) => setBasis(event.target.value)}
            >
              <option value="all">全部</option>
              <option value="provider_reported">provider 上报</option>
              <option value="estimated">本地估算</option>
              <option value="mixed">混合</option>
            </select>
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
            {!filteredAttempts.length && (
              <TableRow>
                <TableCell
                  colSpan={6}
                  className="text-center text-muted-foreground"
                >
                  暂无匹配的用量记录。
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </AnimatedCard>
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
