import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"

import { AnimatedCard } from "@/components/animated-card"
import { Button } from "@/components/coss/components/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/coss/components/table"
import { Overlay } from "@/components/coss/overlay"
import { PageHeader } from "@/components/page-header"
import { api } from "@/lib/api"
import { useOverlaySearch } from "@/lib/search-params"

export function ModelsPage() {
  const queryClient = useQueryClient()
  const [priorityId, setPriorityId] = useOverlaySearch("priority")
  const [evidenceId, setEvidenceId] = useOverlaySearch("evidence")
  const [order, setOrder] = useState<string[]>([])
  const upstreams = useQuery({
    queryKey: ["upstreams"],
    queryFn: api.listUpstreams,
  })
  const models = useQuery({ queryKey: ["models"], queryFn: api.listModels })
  const detail = useQuery({
    queryKey: ["model-detail", evidenceId],
    queryFn: () => api.getModel(evidenceId ?? ""),
    enabled: Boolean(evidenceId),
  })
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["models"] })
  }
  const success = (message: string) => {
    toast.success(message)
  }
  const saveRouting = useMutation({
    mutationFn: ({ id, ids }: { id: string; ids: string[] }) =>
      api.putModelRouting(id, ids),
    onSuccess: () => {
      setPriorityId(null)
      success("模型优先级已保存。")
      invalidate()
    },
    onError: (e: Error) => toast.error(e.message),
  })
  const rows = models.data ?? []
  const selectedModel = rows.find((row) => row.id === priorityId)

  function move(id: string, direction: -1 | 1) {
    const index = order.indexOf(id)
    const next = index + direction
    if (index < 0 || next < 0 || next >= order.length) return
    const reordered = [...order]
    ;[reordered[index], reordered[next]] = [reordered[next], reordered[index]]
    setOrder(reordered)
  }

  function openPriority(modelId: string, upstreamNames: string[]) {
    setOrder(
      upstreams.data
        ?.filter((u) => upstreamNames.includes(u.name))
        .map((u) => u.id) ?? [],
    )
    setPriorityId(modelId)
  }

  return (
    <section aria-labelledby="models-heading" className="space-y-4">
      <PageHeader
        title="模型"
        description="系统自动聚合已匹配的规范模型，不提供逐模型启用/禁用开关。"
      />
      <AnimatedCard
        title="规范模型"
        description="可用上游数与优先级摘要来自上游聚合。"
        beam
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>模型</TableHead>
              <TableHead>可用上游数</TableHead>
              <TableHead>元数据</TableHead>
              <TableHead>优先级</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((model) => (
              <TableRow key={model.id}>
                <TableCell>{model.display_name}</TableCell>
                <TableCell>{model.upstream_count}</TableCell>
                <TableCell>
                  {model.metadata_status === "complete" ? "完整" : "缺失"}
                </TableCell>
                <TableCell>{model.priority_summary}</TableCell>
                <TableCell className="space-x-2 text-right">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setEvidenceId(model.id)}
                  >
                    证据
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => openPriority(model.id, model.upstream_names)}
                  >
                    优先级
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {!rows.length && (
              <TableRow>
                <TableCell
                  colSpan={5}
                  className="text-center text-muted-foreground"
                >
                  暂无可用规范模型。
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </AnimatedCard>
      <Overlay
        open={Boolean(priorityId)}
        onClose={() => setPriorityId(null)}
        title={`优先级管理：${selectedModel?.display_name ?? ""}`}
      >
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            用上移/下移设置模型的上游尝试顺序。
          </p>
          <ul aria-label="上游优先级" className="space-y-2">
            {order.map((id, index) => {
              const upstream = upstreams.data?.find((item) => item.id === id)
              return (
                <li
                  key={id}
                  className="flex items-center justify-between rounded-md border p-2 text-sm"
                >
                  <span>
                    {index + 1}. {upstream?.name ?? id}
                  </span>
                  <span className="space-x-1">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={index === 0}
                      onClick={() => move(id, -1)}
                    >
                      上移
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={index === order.length - 1}
                      onClick={() => move(id, 1)}
                    >
                      下移
                    </Button>
                  </span>
                </li>
              )
            })}
            {!order.length && (
              <li className="text-sm text-muted-foreground">
                暂无可排序上游。
              </li>
            )}
          </ul>
          <Button
            disabled={saveRouting.isPending || !order.length}
            onClick={() =>
              priorityId && saveRouting.mutate({ id: priorityId, ids: order })
            }
          >
            {saveRouting.isPending ? "保存中…" : "保存优先级"}
          </Button>
        </div>
      </Overlay>
      <Overlay
        open={Boolean(evidenceId)}
        onClose={() => setEvidenceId(null)}
        title={`模型证据：${detail.data?.model.display_name ?? ""}`}
        variant="sheet"
      >
        <div className="space-y-6">
          <section aria-label="身份证据">
            <h3 className="mb-2 font-medium">身份证据</h3>
            {(detail.data?.identity_evidence ?? []).map((item) => (
              <div
                key={JSON.stringify(item)}
                className="rounded-md border p-3 text-sm"
              >
                <pre className="whitespace-pre-wrap break-all">
                  {JSON.stringify(item, null, 2)}
                </pre>
              </div>
            ))}
            {!detail.data?.identity_evidence.length && (
              <p className="text-sm text-muted-foreground">暂无身份证据。</p>
            )}
          </section>
          <section aria-label="目录候选与剔除原因">
            <h3 className="mb-2 font-medium">目录候选与剔除原因</h3>
            {(detail.data?.catalog_candidates ?? []).map((candidate) => (
              <div
                key={JSON.stringify(candidate)}
                className="rounded-md border p-3 text-sm"
              >
                <pre className="whitespace-pre-wrap break-all">
                  {JSON.stringify(candidate, null, 2)}
                </pre>
              </div>
            ))}
            {!detail.data?.catalog_candidates.length && (
              <p className="text-sm text-muted-foreground">
                暂无目录候选证据。
              </p>
            )}
          </section>
          <section aria-label="字段级证据">
            <h3 className="mb-2 font-medium">字段级证据</h3>
            {(detail.data?.catalog_evidence ?? []).flat().map((field) => {
              const value = field as {
                field_path?: string
                verification_status?: string
                resolution_reason?: string
                advice?: string
                observed_at?: string
              }
              return (
                <div
                  key={JSON.stringify(value)}
                  className="rounded-md border p-3"
                >
                  <p className="font-medium">
                    {value.field_path ?? "未知字段"}
                  </p>
                  <p className="text-sm text-muted-foreground">
                    {value.verification_status ?? "-"}
                  </p>
                  {value.resolution_reason && (
                    <p className="mt-1 text-sm">
                      原因：{value.resolution_reason}
                    </p>
                  )}
                  {value.advice && (
                    <p className="text-sm">建议：{value.advice}</p>
                  )}
                  <p className="mt-1 text-xs text-muted-foreground">
                    {value.observed_at}
                  </p>
                </div>
              )
            })}
            {!detail.data?.catalog_evidence.length && (
              <p className="text-sm text-muted-foreground">暂无字段级证据。</p>
            )}
          </section>
        </div>
      </Overlay>
    </section>
  )
}
