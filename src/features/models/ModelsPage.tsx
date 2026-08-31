import { arrayMove } from "@dnd-kit/helpers"
import { DragDropProvider, type DragEndEvent } from "@dnd-kit/react"
import { isSortable, useSortable } from "@dnd-kit/react/sortable"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { GripVertical } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { AnimatedCard } from "@/components/animated-card"
import { Button } from "@/components/coss/components/button"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/coss/components/popover"
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
import { cn } from "@/lib/utils"

function SortableItem({
  id,
  index,
  label,
}: {
  id: string
  index: number
  label: string
}) {
  const { ref, handleRef, isDragging } = useSortable({ id, index })
  return (
    <li
      ref={ref}
      className={cn(
        "flex items-center gap-3 rounded-md border p-2 text-sm transition-shadow",
        isDragging && "opacity-40 shadow-lg",
      )}
    >
      <button
        ref={handleRef}
        type="button"
        aria-label={`拖动 ${label} 调整顺序`}
        className="cursor-grab touch-none rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground active:cursor-grabbing"
      >
        <GripVertical className="size-4" />
      </button>
      <span className="text-muted-foreground">{index + 1}.</span>
      <span>{label}</span>
    </li>
  )
}

export function ModelsPage() {
  const queryClient = useQueryClient()
  const [evidenceId, setEvidenceId] = useOverlaySearch("evidence")
  const [order, setOrder] = useState<string[]>([])
  const [priorityModel, setPriorityModel] = useState<string | null>(null)
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
  const saveRouting = useMutation({
    mutationFn: ({ id, ids }: { id: string; ids: string[] }) =>
      api.putModelRouting(id, ids),
    onSuccess: () => {
      setPriorityModel(null)
      toast.success("模型优先级已保存。")
      invalidate()
    },
    onError: (e: Error) => toast.error(e.message),
  })
  const rows = models.data ?? []

  function openPriority(modelId: string, upstreamNames: string[]) {
    setOrder(
      upstreams.data
        ?.filter((u) => upstreamNames.includes(u.name))
        .map((u) => u.id) ?? [],
    )
    setPriorityModel(modelId)
  }

  function handleDragEnd(event: DragEndEvent) {
    if (event.canceled) return
    const { source } = event.operation
    if (!isSortable(source)) return
    const from = source.initialIndex
    const to = source.index
    if (from === to) return
    setOrder((prev) => arrayMove(prev, from, to))
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
                  <Popover
                    open={priorityModel === model.id}
                    onOpenChange={(open: boolean) => {
                      if (open) openPriority(model.id, model.upstream_names)
                      else setPriorityModel(null)
                    }}
                  >
                    <PopoverTrigger
                      render={(props) => (
                        <Button {...props} size="sm" variant="outline">
                          优先级
                        </Button>
                      )}
                    />
                    <PopoverContent className="w-80" align="end">
                      <p className="mb-2 text-sm font-medium">
                        {model.display_name}
                      </p>
                      <p className="mb-3 text-xs text-muted-foreground">
                        拖拽手柄调整上游尝试顺序。
                      </p>
                      <DragDropProvider onDragEnd={handleDragEnd}>
                        <ul
                          aria-label="上游优先级"
                          className="max-h-64 space-y-2 overflow-y-auto"
                        >
                          {order.map((id, index) => {
                            const upstream = upstreams.data?.find(
                              (item) => item.id === id,
                            )
                            return (
                              <SortableItem
                                key={id}
                                id={id}
                                index={index}
                                label={upstream?.name ?? id}
                              />
                            )
                          })}
                          {!order.length && (
                            <li className="text-sm text-muted-foreground">
                              暂无可排序上游。
                            </li>
                          )}
                        </ul>
                      </DragDropProvider>
                      <Button
                        className="mt-3 w-full"
                        size="sm"
                        disabled={saveRouting.isPending || !order.length}
                        onClick={() =>
                          priorityModel &&
                          saveRouting.mutate({
                            id: priorityModel,
                            ids: order,
                          })
                        }
                      >
                        {saveRouting.isPending ? "保存中…" : "保存优先级"}
                      </Button>
                    </PopoverContent>
                  </Popover>
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
