import { arrayMove } from "@dnd-kit/helpers"
import {
  DragDropProvider,
  type DragEndEvent,
  DragOverlay,
  type DragStartEvent,
} from "@dnd-kit/react"
import { isSortable, useSortable } from "@dnd-kit/react/sortable"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { GripVertical, Loader2 } from "lucide-react"
import type { PropsWithChildren } from "react"
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { AnimatedCard } from "@/components/animated-card"
import { Alert, AlertDescription } from "@/components/coss/components/alert"
import { Badge } from "@/components/coss/components/badge"
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
import { PageHeader } from "@/components/page-header"
import {
  api,
  type PresetCreateValues,
  type PresetProvider,
  presetCreateSchema,
  type Upstream,
  type UpstreamCreateValues,
  type UpstreamUpdateValues,
  upstreamCreateSchema,
  upstreamUpdateSchema,
} from "@/lib/api"
import { useOverlaySearch } from "@/lib/search-params"
import { cn } from "@/lib/utils"

function SortableRow({
  upstream,
  index,
  children,
}: PropsWithChildren<{ upstream: Upstream; index: number }>) {
  const { ref, handleRef, isDragging } = useSortable({
    id: upstream.id,
    index,
  })
  return (
    <TableRow ref={ref} className={cn(isDragging && "opacity-40")}>
      <TableCell className="w-10 pr-0">
        <button
          ref={handleRef}
          type="button"
          aria-label={`拖动 ${upstream.name} 调整顺序`}
          className="cursor-grab touch-none rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground active:cursor-grabbing"
        >
          <GripVertical className="size-4" />
        </button>
      </TableCell>
      {children}
    </TableRow>
  )
}

export function UpstreamsPage() {
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useOverlaySearch("create")
  const [editId, setEditId] = useOverlaySearch("edit")
  const [detailId, setDetailId] = useOverlaySearch("detail")
  const [createMode, setCreateMode] = useState<"preset" | "custom">("custom")
  const [presetForm, setPresetForm] = useState<PresetCreateValues>({
    preset_id: "",
    api_credential: "",
  })
  const [createForm, setCreateForm] = useState<UpstreamCreateValues>({
    name: "",
    base_url: "",
    api_credential: "",
  })
  const [editForm, setEditForm] = useState<UpstreamUpdateValues>({
    name: "",
    base_url: "",
    api_credential: "",
  })
  const [error, setError] = useState<string | null>(null)
  const [globalOrder, setGlobalOrder] = useState<string[]>([])
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const query = useQuery({
    queryKey: ["upstreams"],
    queryFn: api.listUpstreams,
  })
  const presets = useQuery({
    queryKey: ["presets"],
    queryFn: api.listPresets,
  })
  const offerings = useQuery({
    queryKey: ["upstream-offerings", detailId],
    queryFn: () => api.listUpstreamOfferings(detailId ?? ""),
    enabled: Boolean(detailId),
  })
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["upstreams"] })
  }
  const success = (message: string) => {
    toast.success(message)
  }
  const create = useMutation({
    mutationFn: api.createUpstream,
    onSuccess: () => {
      setCreateOpen(null)
      setCreateMode("custom")
      setPresetForm({ preset_id: "", api_credential: "" })
      setCreateForm({ name: "", base_url: "", api_credential: "" })
      success("上游已创建并完成探测。")
      invalidate()
    },
    onError: (e: Error) => toast.error(e.message),
  })
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: UpstreamUpdateValues }) =>
      api.updateUpstream(id, body),
    onSuccess: () => {
      setEditId(null)
      success("上游已保存。")
      invalidate()
    },
    onError: (e: Error) => toast.error(e.message),
  })
  const updateStatus = useMutation({
    mutationFn: ({
      id,
      status,
    }: {
      id: string
      status: "enabled" | "disabled"
    }) => api.updateUpstreamStatus(id, status),
    onSuccess: (_data, variables) => {
      success(variables.status === "enabled" ? "上游已启用。" : "上游已禁用。")
      invalidate()
    },
    onError: (e: Error) => toast.error(e.message),
  })
  const remove = useMutation({
    mutationFn: api.deleteUpstream,
    onSuccess: (_data, variables) => {
      success("上游已删除。")
      if (variables === detailId) setDetailId(null)
      if (variables === editId) setEditId(null)
      invalidate()
      void queryClient.invalidateQueries({ queryKey: ["routing"] })
      void queryClient.invalidateQueries({ queryKey: ["models"] })
    },
    onError: (e: Error) => toast.error(e.message),
  })
  const routing = useQuery({
    queryKey: ["routing"],
    queryFn: api.listRouting,
  })
  const rows = query.data ?? []

  useEffect(() => {
    if (!rows.length) {
      setGlobalOrder([])
      return
    }
    const global = routing.data?.find((item) => item.scope === "global")
    const orderedIds = global?.ordered_upstream_ids ?? []
    setGlobalOrder([
      ...orderedIds.filter((id) => rows.some((row) => row.id === id)),
      ...rows.map((row) => row.id).filter((id) => !orderedIds.includes(id)),
    ])
  }, [routing.data, rows])

  const saveGlobalRouting = useMutation({
    mutationFn: api.putGlobalRouting,
    onSuccess: () => {
      success("全局上游顺序已保存。")
      void queryClient.invalidateQueries({ queryKey: ["routing"] })
    },
    onError: (e: Error) => toast.error(e.message),
  })
  const editing = rows.find((row) => row.id === editId)
  const detail = rows.find((row) => row.id === detailId)
  const discovery = useQuery({
    queryKey: ["preset-discovery", detailId],
    queryFn: () => api.getPresetDiscovery(detailId ?? ""),
    enabled: detail?.kind === "preset",
  })

  function submitCreate() {
    if (createMode === "preset") {
      const parsed = presetCreateSchema.safeParse(presetForm)
      if (!parsed.success) {
        setError(parsed.error.issues[0]?.message ?? "表单校验失败。")
        return
      }
      setError(null)
      create.mutate({ kind: "preset", ...parsed.data })
      return
    }
    const parsed = upstreamCreateSchema.safeParse(createForm)
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? "表单校验失败。")
      return
    }
    setError(null)
    create.mutate(parsed.data)
  }

  function openEdit(id: string, name: string, baseUrl: string) {
    setEditForm({ name, base_url: baseUrl, api_credential: "" })
    setEditId(id)
  }

  function handleDragStart(event: DragStartEvent) {
    const id = event.operation.source?.id
    if (id != null) setDraggingId(String(id))
  }

  function handleDragEnd(event: DragEndEvent) {
    setDraggingId(null)
    if (event.canceled) return
    const { source } = event.operation
    if (!isSortable(source)) return
    const from = source.initialIndex
    const to = source.index
    if (from === to) return
    const nextOrder = arrayMove(globalOrder, from, to)
    setGlobalOrder(nextOrder)
    saveGlobalRouting.mutate(nextOrder)
  }

  function submitEdit() {
    if (!editing) return
    const parsed = upstreamUpdateSchema.safeParse(editForm)
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? "表单校验失败。")
      return
    }
    update.mutate({ id: editing.id, body: parsed.data })
  }

  return (
    <section aria-labelledby="upstreams-heading" className="space-y-4">
      <PageHeader
        title="上游管理"
        description="列表优先管理上游连接，复杂操作在覆盖层中完成。"
        actions={
          <Button type="button" onClick={() => setCreateOpen(true)}>
            创建上游
          </Button>
        }
      />
      <AnimatedCard
        title="上游列表"
        description="拖动行首手柄调整默认路由尝试顺序；系统不在此页面展示协议名称或确认状态。"
      >
        {query.isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">加载中…</div>
        ) : query.isError ? (
          <Alert variant="error">
            <AlertDescription>上游列表加载失败。</AlertDescription>
          </Alert>
        ) : (
          <DragDropProvider
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <span className="sr-only">排序</span>
                  </TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>健康摘要</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {globalOrder.map((id, index) => {
                  const upstream = rows.find((row) => row.id === id)
                  if (!upstream) return null
                  return (
                    <SortableRow
                      key={upstream.id}
                      upstream={upstream}
                      index={index}
                    >
                      <TableCell className="font-medium">
                        {upstream.name}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={
                            upstream.status === "enabled"
                              ? "default"
                              : "secondary"
                          }
                        >
                          {upstream.status === "enabled" ? "启用" : "禁用"}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {upstream.protocol_probe_summary ||
                          upstream.last_health_result ||
                          "暂无证据"}
                      </TableCell>
                      <TableCell className="space-x-2 text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setDetailId(upstream.id)}
                        >
                          详情
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            openEdit(
                              upstream.id,
                              upstream.name,
                              upstream.base_url,
                            )
                          }
                        >
                          编辑
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={
                            updateStatus.isPending &&
                            updateStatus.variables?.id === upstream.id
                          }
                          onClick={() =>
                            updateStatus.mutate({
                              id: upstream.id,
                              status:
                                upstream.status === "enabled"
                                  ? "disabled"
                                  : "enabled",
                            })
                          }
                        >
                          {updateStatus.isPending &&
                          updateStatus.variables?.id === upstream.id ? (
                            <Loader2 className="size-4 animate-spin" />
                          ) : null}
                          {upstream.status === "enabled" ? "禁用" : "启用"}
                        </Button>
                        <Button
                          size="sm"
                          variant="destructive"
                          disabled={
                            remove.isPending && remove.variables === upstream.id
                          }
                          onClick={() => {
                            if (
                              window.confirm(
                                `确认删除上游「${upstream.name}」？该操作不可恢复。`,
                              )
                            ) {
                              remove.mutate(upstream.id)
                            }
                          }}
                        >
                          {remove.isPending &&
                          remove.variables === upstream.id ? (
                            <Loader2 className="size-4 animate-spin" />
                          ) : null}
                          删除
                        </Button>
                      </TableCell>
                    </SortableRow>
                  )
                })}
                {!globalOrder.length && (
                  <TableRow>
                    <TableCell
                      colSpan={5}
                      className="text-center text-muted-foreground"
                    >
                      暂无上游，请先创建。
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
            <DragOverlay>
              {draggingId && (
                <div className="rounded-md border bg-card px-3 py-2 text-sm shadow-lg">
                  {rows.find((row) => row.id === draggingId)?.name ??
                    draggingId}
                </div>
              )}
            </DragOverlay>
          </DragDropProvider>
        )}
      </AnimatedCard>
      <Overlay
        open={Boolean(createOpen)}
        onClose={() => setCreateOpen(null)}
        title="创建上游"
      >
        <form
          aria-label="创建上游"
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            submitCreate()
          }}
        >
          <div className="grid gap-1.5">
            <Label>创建方式</Label>
            <div className="flex gap-2">
              <Button
                type="button"
                variant={createMode === "preset" ? "default" : "outline"}
                onClick={() => setCreateMode("preset")}
              >
                预设 Provider
              </Button>
              <Button
                type="button"
                variant={createMode === "custom" ? "default" : "outline"}
                onClick={() => setCreateMode("custom")}
              >
                自定义 Provider
              </Button>
            </div>
          </div>

          {createMode === "preset" ? (
            <>
              <div className="grid gap-1.5">
                <Label htmlFor="preset-select">预设 Provider</Label>
                <select
                  id="preset-select"
                  value={presetForm.preset_id}
                  onChange={(e) =>
                    setPresetForm({
                      ...presetForm,
                      preset_id: e.target.value,
                    })
                  }
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm shadow-sm"
                >
                  <option value="">请选择…</option>
                  {(presets.data ?? []).map((preset: PresetProvider) => (
                    <option key={preset.preset_id} value={preset.preset_id}>
                      {preset.name}
                    </option>
                  ))}
                </select>
                {(() => {
                  const selected = (presets.data ?? []).find(
                    (preset) => preset.preset_id === presetForm.preset_id,
                  )
                  if (!selected) return null
                  return (
                    <p className="text-sm text-muted-foreground">
                      {selected.base_url} ·
                      创建/探测时从官方文档自动获取模型列表
                    </p>
                  )
                })()}
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="preset-key">API 凭据</Label>
                <Input
                  id="preset-key"
                  type="password"
                  value={presetForm.api_credential}
                  onChange={(e) =>
                    setPresetForm({
                      ...presetForm,
                      api_credential: e.target.value,
                    })
                  }
                />
              </div>
            </>
          ) : (
            <>
              <div className="grid gap-1.5">
                <Label htmlFor="upstream-name">上游名称</Label>
                <Input
                  id="upstream-name"
                  value={createForm.name}
                  onChange={(e) =>
                    setCreateForm({ ...createForm, name: e.target.value })
                  }
                />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="upstream-url">Base URL</Label>
                <Input
                  id="upstream-url"
                  value={createForm.base_url}
                  onChange={(e) =>
                    setCreateForm({ ...createForm, base_url: e.target.value })
                  }
                />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="upstream-key">API 凭据</Label>
                <Input
                  id="upstream-key"
                  type="password"
                  value={createForm.api_credential}
                  onChange={(e) =>
                    setCreateForm({
                      ...createForm,
                      api_credential: e.target.value,
                    })
                  }
                />
              </div>
            </>
          )}
          {error && <p className="text-sm text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setCreateOpen(null)}
            >
              取消
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? "创建中…" : "创建并探测"}
            </Button>
          </div>
        </form>
      </Overlay>
      <Overlay
        open={Boolean(editing)}
        onClose={() => setEditId(null)}
        title="编辑上游"
      >
        <form
          aria-label="编辑上游"
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            submitEdit()
          }}
        >
          <div className="grid gap-1.5">
            <Label htmlFor="edit-upstream-name">上游名称</Label>
            <Input
              id="edit-upstream-name"
              value={editForm.name}
              onChange={(e) =>
                setEditForm({ ...editForm, name: e.target.value })
              }
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="edit-upstream-url">Base URL</Label>
            <Input
              id="edit-upstream-url"
              value={editForm.base_url}
              onChange={(e) =>
                setEditForm({ ...editForm, base_url: e.target.value })
              }
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="edit-upstream-key">API 凭据</Label>
            <Input
              id="edit-upstream-key"
              type="password"
              value={editForm.api_credential ?? ""}
              onChange={(e) =>
                setEditForm({ ...editForm, api_credential: e.target.value })
              }
            />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setEditId(null)}
            >
              取消
            </Button>
            <Button type="submit" disabled={update.isPending}>
              {update.isPending ? "保存中…" : "保存"}
            </Button>
          </div>
        </form>
      </Overlay>
      <Overlay
        open={Boolean(detailId)}
        onClose={() => setDetailId(null)}
        title={`上游详情：${detail?.name ?? ""}`}
        variant="sheet"
      >
        <div className="space-y-6">
          <section aria-label="探测诊断">
            <h3 className="mb-2 font-medium">探测诊断</h3>
            <p className="text-sm">
              {detail?.protocol_probe_summary ||
                detail?.last_health_result ||
                "暂无证据"}
            </p>
            {detail?.connectivity_probe && (
              <pre className="mt-2 whitespace-pre-wrap break-all rounded-md border p-3 text-sm">
                {JSON.stringify(detail.connectivity_probe, null, 2)}
              </pre>
            )}
          </section>
          {detail?.kind === "preset" && (
            <section aria-label="模型目录发现">
              <h3 className="mb-2 font-medium">模型目录发现</h3>
              {discovery.isLoading && (
                <p className="text-sm text-muted-foreground">加载中…</p>
              )}
              {discovery.isError && (
                <p className="text-sm text-destructive">
                  模型目录状态加载失败。
                </p>
              )}
              {discovery.data && (
                <div className="space-y-1 text-sm">
                  <p>
                    状态：
                    {discovery.data.status === "succeeded"
                      ? "成功"
                      : discovery.data.status === "failed"
                        ? "失败，当前列表已清空"
                        : "尚未探测"}
                  </p>
                  <p>当前模型数：{discovery.data.current_model_count}</p>
                  {discovery.data.failures[
                    discovery.data.failures.length - 1
                  ] && (
                    <p className="text-destructive">
                      最近失败：
                      {String(
                        discovery.data.failures[
                          discovery.data.failures.length - 1
                        ].failure_message ?? "未知错误",
                      )}
                    </p>
                  )}
                </div>
              )}
            </section>
          )}
          <section aria-label="offering 管理">
            <h3 className="mb-2 font-medium">Offering 管理</h3>
            {offerings.isLoading && (
              <p className="text-sm text-muted-foreground">加载中…</p>
            )}
            {offerings.data && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>模型</TableHead>
                    <TableHead>状态</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[
                    ...new Map(
                      offerings.data.map((offering) => [
                        offering.provider_model_id,
                        offering,
                      ]),
                    ).values(),
                  ].map((offering) => (
                    <TableRow key={offering.provider_model_id}>
                      <TableCell>{offering.provider_model_id}</TableCell>
                      <TableCell>
                        {offering.status === "approved"
                          ? "已启用"
                          : offering.status === "disabled"
                            ? "已禁用"
                            : "待确认"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {offerings.data && !offerings.data.length && (
              <p className="text-sm text-muted-foreground">暂无 offering。</p>
            )}
          </section>
        </div>
      </Overlay>
    </section>
  )
}
