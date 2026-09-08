import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Lock,
  RefreshCw,
  SkipForward,
} from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Badge } from "@/components/coss/components/badge"
import { Button } from "@/components/coss/components/button"
import { PageHeader } from "@/components/page-header"
import { api, type UpdatePolicy } from "@/lib/api"
import { cn } from "@/lib/utils"

const POLICY_LABELS: Record<
  UpdatePolicy,
  { title: string; description: string }
> = {
  notify: {
    title: "仅通知",
    description: "只检查新版本，是否安装由你手动确认。",
  },
  auto: {
    title: "自动更新",
    description: "发现新版本后自动下载、校验并安装。",
  },
  pinned: {
    title: "锁定版本",
    description: "始终保持在指定版本，不接受自动升级。",
  },
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function InstallBadge({ status }: { status: string }) {
  if (status === "running") return <Badge variant="secondary">安装中</Badge>
  if (status === "succeeded")
    return (
      <Badge className="bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
        成功
      </Badge>
    )
  if (status === "failed")
    return <Badge className="bg-destructive/15 text-destructive">失败</Badge>
  return <Badge variant="secondary">空闲</Badge>
}

export function UpdatePage() {
  const queryClient = useQueryClient()
  const [pinnedInput, setPinnedInput] = useState("")

  const status = useQuery({
    queryKey: ["update-status"],
    queryFn: api.getUpdateStatus,
    refetchInterval: 15000,
  })

  const apply = (data: Awaited<ReturnType<typeof api.getUpdateStatus>>) => {
    queryClient.setQueryData(["update-status"], data)
  }

  const check = useMutation({
    mutationFn: api.checkUpdate,
    onSuccess: (data) => {
      apply(data)
      if (data.last_check_error) toast.error(data.last_check_error)
      else toast.success("已刷新版本清单。")
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const run = useMutation({
    mutationFn: (force: boolean) => api.runUpdate(force),
    onSuccess: (data) => {
      apply(data)
      toast[data.started ? "success" : "info"](data.message ?? "已处理。")
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const policy = useMutation({
    mutationFn: api.setUpdatePolicy,
    onSuccess: (data) => {
      apply(data)
      toast.success("更新策略已保存。")
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const data = status.data
  const current = data?.current_version ?? "未知"
  const latest = data?.latest_version ?? "未知"
  const policyValue = data?.policy ?? "notify"

  const changePolicy = (next: UpdatePolicy) => {
    if (next === "pinned") {
      const target = pinnedInput.trim() || data?.latest_version || current
      policy.mutate({ policy: "pinned", pinned_version: target })
      return
    }
    policy.mutate({ policy: next })
  }

  return (
    <section className="space-y-5">
      <PageHeader
        title="网关更新"
        description="基于 Release 清单 + sha256 校验的事务化自更新，可通知、自动或锁定版本。"
        actions={
          <Button
            type="button"
            variant="outline"
            disabled={check.isPending}
            onClick={() => check.mutate()}
          >
            <RefreshCw
              className={cn("mr-2 size-4", check.isPending && "animate-spin")}
            />
            检查更新
          </Button>
        }
      />

      {!data?.managed ? (
        <div className="flex items-start gap-2 border bg-muted/40 p-4 text-sm text-muted-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>
            当前进程不是 systemd 托管部署（未找到 <code>releases/</code> 与{" "}
            <code>deployed-version</code>），自更新仅在被部署的服务器上可用。
          </span>
        </div>
      ) : null}

      {data?.last_check_error ? (
        <div className="flex items-start gap-2 border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>清单检查失败：{data.last_check_error}</span>
        </div>
      ) : null}

      {data?.install_error ? (
        <div className="flex items-start gap-2 border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>上次安装失败：{data.install_error}</span>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-3 border bg-card p-4">
          <h2 className="font-medium">版本状态</h2>
          <dl className="space-y-2 text-sm">
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">当前版本</dt>
              <dd className="font-mono">{current}</dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">最新版本</dt>
              <dd className="flex items-center gap-2 font-mono">
                {latest}
                {data?.update_available ? (
                  <Badge className="bg-primary/15 text-primary">可更新</Badge>
                ) : null}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">构建标识</dt>
              <dd className="font-mono text-xs">
                {data?.current_build ?? "—"}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">最近检查</dt>
              <dd>{formatTime(data?.last_check_at)}</dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">安装状态</dt>
              <dd className="flex items-center gap-2">
                <InstallBadge status={data?.install_status ?? "idle"} />
                {data?.install_version ? (
                  <span className="font-mono text-xs">
                    {data.install_version}
                  </span>
                ) : null}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">保留 release</dt>
              <dd>{data?.retained_releases ?? 0}</dd>
            </div>
          </dl>

          <div className="flex flex-wrap gap-2 pt-1">
            <Button
              type="button"
              disabled={
                !data?.managed || run.isPending || !data?.update_available
              }
              onClick={() => run.mutate(false)}
            >
              <Download className="mr-2 size-4" />
              {data?.target_version ? `升级到 ${data.target_version}` : "升级"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!data?.managed || run.isPending}
              onClick={() => run.mutate(true)}
            >
              重新安装
            </Button>
            {data?.notes_url ? (
              <a
                href={data.notes_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-9 items-center border px-3 text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                查看 Release Notes
              </a>
            ) : null}
          </div>
        </div>

        <div className="space-y-3 border bg-card p-4">
          <h2 className="font-medium">更新策略</h2>
          <div className="space-y-2">
            {(Object.keys(POLICY_LABELS) as UpdatePolicy[]).map((key) => {
              const item = POLICY_LABELS[key]
              const active = policyValue === key
              return (
                <button
                  key={key}
                  type="button"
                  disabled={policy.isPending}
                  onClick={() => changePolicy(key)}
                  className={cn(
                    "flex w-full items-start gap-3 border p-3 text-left transition-colors",
                    active
                      ? "border-primary/50 bg-primary/10"
                      : "hover:bg-muted/60",
                  )}
                >
                  <span className="mt-0.5">
                    {active ? (
                      <CheckCircle2 className="size-4 text-primary" />
                    ) : (
                      <span className="block size-4 rounded-full border" />
                    )}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm font-medium">
                      {item.title}
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {item.description}
                    </span>
                  </span>
                </button>
              )
            })}
          </div>

          <div className="space-y-2 border-t pt-3">
            <label
              htmlFor="pinned-version"
              className="block text-xs text-muted-foreground"
            >
              锁定版本（pinned 策略使用）
            </label>
            <div className="flex gap-2">
              <input
                id="pinned-version"
                value={pinnedInput}
                onChange={(event) => setPinnedInput(event.target.value)}
                placeholder={data?.pinned_version ?? "例如 v0.2.27"}
                className="h-9 min-w-0 flex-1 border bg-background px-3 font-mono text-sm outline-none focus:border-primary"
              />
              <Button
                type="button"
                variant="outline"
                disabled={policy.isPending}
                onClick={() => changePolicy("pinned")}
              >
                <Lock className="mr-2 size-4" />
                锁定
              </Button>
            </div>
            <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
              {data?.policy === "pinned" && data.pinned_version ? (
                <span>当前锁定：{data.pinned_version}</span>
              ) : null}
              {data?.dismissed_version ? (
                <span>已跳过：{data.dismissed_version}</span>
              ) : null}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!data?.latest_version || policy.isPending}
                onClick={() =>
                  policy.mutate({
                    policy: policyValue === "pinned" ? "notify" : policyValue,
                    dismissed_version: data?.latest_version ?? "",
                  })
                }
              >
                <SkipForward className="mr-2 size-3.5" />
                跳过最新版本
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={policy.isPending}
                onClick={() =>
                  policy.mutate({ policy: policyValue, dismissed_version: "" })
                }
              >
                清除跳过
              </Button>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
