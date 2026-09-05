import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2, Pencil, Plus, Trash2 } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Alert, AlertDescription } from "@/components/coss/components/alert"
import { Badge } from "@/components/coss/components/badge"
import { Button } from "@/components/coss/components/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/coss/components/card"
import {
  Dialog,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPanel,
  DialogPopup,
  DialogTitle,
} from "@/components/coss/components/dialog"
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
import { Textarea } from "@/components/coss/components/textarea"
import { api, type CodexMcpServer } from "@/lib/api"

type McpDraft = {
  name: string
  url: string
  command: string
  args: string
  env: string
}

const EMPTY_DRAFT: McpDraft = {
  name: "",
  url: "",
  command: "",
  args: "",
  env: "",
}

function draftFromServer(server: CodexMcpServer): McpDraft {
  return {
    name: server.name,
    url: server.url ?? "",
    command: server.command ?? "",
    args: server.args.join(" "),
    env: "",
  }
}

function parseEnvLines(text: string): Record<string, string> | null {
  const env: Record<string, string> = {}
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed) {
      continue
    }
    const sep = trimmed.indexOf("=")
    if (sep <= 0) {
      return null
    }
    env[trimmed.slice(0, sep).trim()] = trimmed.slice(sep + 1).trim()
  }
  return env
}

function McpEditorDialog({
  draft,
  isEdit,
  onClose,
}: {
  draft: McpDraft | null
  isEdit: boolean
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<McpDraft>(draft ?? EMPTY_DRAFT)

  const save = useMutation({
    mutationFn: () => {
      const env = parseEnvLines(form.env)
      if (env === null) {
        throw new Error("环境变量每行必须是 KEY=VALUE 格式。")
      }
      return api.upsertCodexMcpServer({
        name: form.name.trim(),
        url: form.url.trim() || undefined,
        command: form.command.trim() || undefined,
        args: form.args.trim() ? form.args.trim().split(/\s+/) : undefined,
        env: Object.keys(env).length ? env : undefined,
      })
    },
    onSuccess: () => {
      toast.success("MCP 服务器已保存。")
      void queryClient.invalidateQueries({ queryKey: ["codex-mcp-servers"] })
      onClose()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  if (!draft) {
    return null
  }
  const canSave =
    form.name.trim().length > 0 && !!form.url.trim() !== !!form.command.trim()

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogPopup className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? "编辑 MCP 服务器" : "新增 MCP 服务器"}
          </DialogTitle>
          <DialogDescription>
            远程 URL 与本地命令二选一，写入本机 Codex config.toml 的
            [mcp_servers]。
          </DialogDescription>
        </DialogHeader>
        <DialogPanel className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="mcp-name">名称</Label>
            <Input
              id="mcp-name"
              value={form.name}
              disabled={isEdit}
              onChange={(event) =>
                setForm({ ...form, name: event.target.value })
              }
              placeholder="context7"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mcp-url">远程 URL</Label>
            <Input
              id="mcp-url"
              value={form.url}
              onChange={(event) =>
                setForm({ ...form, url: event.target.value })
              }
              placeholder="https://mcp.firecrawl.dev/v2/mcp"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mcp-command">本地命令</Label>
            <Input
              id="mcp-command"
              value={form.command}
              onChange={(event) =>
                setForm({ ...form, command: event.target.value })
              }
              placeholder="npx"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mcp-args">参数（空格分隔）</Label>
            <Input
              id="mcp-args"
              value={form.args}
              onChange={(event) =>
                setForm({ ...form, args: event.target.value })
              }
              placeholder="-y @playwright/mcp@latest --headless"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mcp-env">环境变量（每行 KEY=VALUE）</Label>
            <Textarea
              id="mcp-env"
              rows={3}
              value={form.env}
              onChange={(event) =>
                setForm({ ...form, env: event.target.value })
              }
              placeholder={"GITHUB_TOKEN=ghp_xxx"}
            />
          </div>
        </DialogPanel>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={!canSave || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? (
              <Loader2 className="size-4 animate-spin" />
            ) : null}
            保存
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  )
}

export function CodexMcpSubPage() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<{
    draft: McpDraft
    isEdit: boolean
  } | null>(null)

  const query = useQuery({
    queryKey: ["codex-mcp-servers"],
    queryFn: api.listCodexMcpServers,
  })

  const remove = useMutation({
    mutationFn: api.deleteCodexMcpServer,
    onSuccess: () => {
      toast.success("MCP 服务器已删除。")
      void queryClient.invalidateQueries({ queryKey: ["codex-mcp-servers"] })
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const data = query.data

  return (
    <>
      {query.isLoading ? (
        <div className="text-sm text-muted-foreground">加载中…</div>
      ) : query.isError ? (
        <Alert variant="error">
          <AlertDescription>Codex MCP 配置加载失败。</AlertDescription>
        </Alert>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>MCP 服务器</CardTitle>
              <CardDescription>
                config.toml 中 [mcp_servers] 的直连注册项，共{" "}
                {data?.servers.length ?? 0} 个。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div>
                <Button
                  size="sm"
                  onClick={() =>
                    setDraft({ draft: EMPTY_DRAFT, isEdit: false })
                  }
                >
                  <Plus className="size-4" />
                  新增
                </Button>
              </div>
              {data?.servers.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>名称</TableHead>
                      <TableHead>类型</TableHead>
                      <TableHead>命令 / URL</TableHead>
                      <TableHead>环境变量</TableHead>
                      <TableHead className="text-right">操作</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.servers.map((server) => (
                      <TableRow key={server.name}>
                        <TableCell className="font-medium">
                          {server.name}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              server.transport === "url" ? "info" : "secondary"
                            }
                          >
                            {server.transport === "url" ? "远程" : "stdio"}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-[320px] truncate font-mono text-xs">
                          {server.url ??
                            [server.command, ...server.args]
                              .filter(Boolean)
                              .join(" ")}
                        </TableCell>
                        <TableCell>
                          {server.env_keys.length ? (
                            <span className="text-xs text-muted-foreground">
                              {server.env_keys.join(", ")}
                            </span>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-1">
                            <Button
                              size="icon-sm"
                              variant="ghost"
                              aria-label={`编辑 ${server.name}`}
                              onClick={() =>
                                setDraft({
                                  draft: draftFromServer(server),
                                  isEdit: true,
                                })
                              }
                            >
                              <Pencil className="size-4" />
                            </Button>
                            <Button
                              size="icon-sm"
                              variant="ghost"
                              disabled={remove.isPending}
                              aria-label={`删除 ${server.name}`}
                              onClick={() => remove.mutate(server.name)}
                            >
                              <Trash2 className="size-4" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <div className="text-sm text-muted-foreground">
                  config.toml 中还没有 MCP 服务器。
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>插件捆绑 MCP</CardTitle>
              <CardDescription>
                已启用插件通过自身 .mcp.json 提供的 MCP
                服务器（只读，随插件启停）。
              </CardDescription>
            </CardHeader>
            <CardContent>
              {data?.plugin_servers.length ? (
                <ul className="space-y-3">
                  {data.plugin_servers.map((plugin) => (
                    <li key={plugin.plugin_id} className="space-y-1.5">
                      <div className="flex items-center gap-2 text-sm font-medium">
                        {plugin.plugin_id}
                        <Badge variant="success">已启用</Badge>
                      </div>
                      <ul className="space-y-1 text-sm text-muted-foreground">
                        {plugin.servers.map((server) => (
                          <li
                            key={server.name}
                            className="flex items-center gap-2"
                          >
                            <Badge variant="outline">{server.name}</Badge>
                            <span className="truncate font-mono text-xs">
                              {server.url ??
                                [server.command, ...server.args]
                                  .filter(Boolean)
                                  .join(" ")}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-sm text-muted-foreground">
                  当前已启用的插件没有捆绑 MCP 服务器。
                </div>
              )}
            </CardContent>
          </Card>

          {data?.config_path ? (
            <p className="text-xs text-muted-foreground">
              配置文件：{data.config_path}
            </p>
          ) : null}
        </>
      )}

      <McpEditorDialog
        draft={draft?.draft ?? null}
        isEdit={draft?.isEdit ?? false}
        onClose={() => setDraft(null)}
      />
    </>
  )
}
