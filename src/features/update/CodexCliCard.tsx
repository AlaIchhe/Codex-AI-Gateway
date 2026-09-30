import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Download, RefreshCw } from "lucide-react"
import type { ReactNode } from "react"
import { toast } from "sonner"

import { Badge } from "@/components/coss/components/badge"
import { Button } from "@/components/coss/components/button"
import { api, type CodexCliStatus } from "@/lib/api"
import { cn } from "@/lib/utils"

const INSTALL_KIND_LABELS: Record<string, string> = {
  npm: "npm 全局安装",
  standalone: "独立二进制",
  unknown: "未知",
}

const UPDATE_STATUS_LABELS: Record<
  string,
  { label: string; variant: "secondary" | "success" | "error" }
> = {
  running: { label: "更新中", variant: "secondary" },
  succeeded: { label: "更新成功", variant: "success" },
  failed: { label: "更新失败", variant: "error" },
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
      <span className="w-20 shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 break-all">{children}</span>
    </div>
  )
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export function CodexCliCard() {
  const queryClient = useQueryClient()
  const status = useQuery({
    queryKey: ["codex-cli-status"],
    queryFn: api.getCodexCliStatus,
    refetchInterval: (query) =>
      query.state.data?.update_status === "running" ? 2000 : 15000,
  })

  const apply = (data: CodexCliStatus) =>
    queryClient.setQueryData(["codex-cli-status"], data)

  const check = useMutation({
    mutationFn: api.checkCodexCli,
    onSuccess: (data) => {
      apply(data)
      if (data.last_check_error) toast.error(data.last_check_error)
      else
        toast.success(
          `已刷新 Codex CLI 最新版本：${data.latest_version ?? "未知"}`,
        )
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const update = useMutation({
    mutationFn: (force: boolean) => api.updateCodexCli(force),
    onSuccess: (data) => {
      apply(data)
      toast[data.started ? "success" : "info"](data.message ?? "已处理。")
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const data = status.data
  const badge = data ? UPDATE_STATUS_LABELS[data.update_status] : undefined
  const localHint = data?.installed_but_broken
    ? "已安装但无法运行"
    : data?.installed
      ? "已安装"
      : "未安装"
  return (
    <div className="space-y-3 rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="font-medium">Codex CLI</h2>
          <p className="text-xs text-muted-foreground">
            检测网关上 <code>codex</code> 命令的版本并升级；独立二进制走 npm
            registry 平台包（校验 integrity 后原子替换并复核版本），npm
            全局安装走 npm 自身升级。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {badge ? <Badge variant={badge.variant}>{badge.label}</Badge> : null}
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={check.isPending}
            onClick={() => check.mutate()}
          >
            <RefreshCw
              className={cn("mr-2 size-3.5", check.isPending && "animate-spin")}
            />
            检查更新
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={!data?.installed || update.isPending}
            onClick={() => update.mutate(data?.update_available === false)}
            title={
              data?.update_available
                ? undefined
                : "当前已是最新版本；点击将强制重装。"
            }
          >
            <Download className="mr-2 size-3.5" />
            {data?.update_target ? `更新到 ${data.update_target}` : "更新"}
          </Button>
        </div>
      </div>

      <div className="space-y-1.5">
        <Row label="状态">
          <span
            className={cn(
              data?.installed_but_broken && "text-destructive-foreground",
            )}
          >
            {localHint}
          </span>
          {data?.update_available ? (
            <span className="ml-2 text-muted-foreground">有可用更新</span>
          ) : null}
        </Row>
        <Row label="当前版本">
          <span className="font-mono">{data?.version ?? "未知"}</span>
        </Row>
        <Row label="最新版本">
          <span className="font-mono">{data?.latest_version ?? "未知"}</span>
          {data?.latest_source ? (
            <span className="ml-2 text-xs text-muted-foreground">
              来源：{data.latest_source}
            </span>
          ) : null}
        </Row>
        <Row label="安装形态">
          {INSTALL_KIND_LABELS[data?.install_kind ?? "unknown"] ??
            data?.install_kind}
        </Row>
        <Row label="路径">
          <span className="font-mono text-xs">{data?.path ?? "—"}</span>
        </Row>
        <Row label="检查时间">{formatTime(data?.last_check_at)}</Row>
      </div>
      {data?.error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span className="break-all">{data.error}</span>
        </div>
      ) : null}

      {data?.last_check_error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span className="break-all">
            版本检查失败：{data.last_check_error}
          </span>
        </div>
      ) : null}

      {data?.update_error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span className="break-all">上次更新失败：{data.update_error}</span>
        </div>
      ) : null}

      {data?.update_log?.length ? (
        <details className="rounded-lg border bg-muted/30 p-3 text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            更新日志（{data.update_log.length} 行）
          </summary>
          <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed">
            {data.update_log.slice(-40).join("\n")}
          </pre>
        </details>
      ) : null}
    </div>
  )
}
