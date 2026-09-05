import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2, Trash2 } from "lucide-react"
import { useState } from "react"
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
import { PageHeader } from "@/components/page-header"
import { api } from "@/lib/api"

export function CodexPluginsPage() {
  const queryClient = useQueryClient()
  const [name, setName] = useState("")
  const [source, setSource] = useState("")
  const [defaultEnabled, setDefaultEnabled] = useState(false)

  const query = useQuery({
    queryKey: ["codex-plugin-marketplaces"],
    queryFn: api.listCodexPluginMarketplaces,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({
      queryKey: ["codex-plugin-marketplaces"],
    })
  }

  const register = useMutation({
    mutationFn: api.registerCodexPluginMarketplace,
    onSuccess: () => {
      setName("")
      setSource("")
      setDefaultEnabled(false)
      toast.success("插件市场已注册。")
      invalidate()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const toggle = useMutation({
    mutationFn: api.toggleCodexPlugin,
    onSuccess: (_data, variables) => {
      toast.success(variables.enabled ? "插件已启用。" : "插件已禁用。")
      invalidate()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const remove = useMutation({
    mutationFn: api.removeCodexPluginMarketplace,
    onSuccess: () => {
      toast.success("插件市场已移除。")
      invalidate()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const data = query.data
  const invalidMarketplaces =
    data?.marketplaces.filter((item) => !item.manifest_valid) ?? []

  return (
    <section aria-labelledby="codex-plugins-heading" className="space-y-4">
      <PageHeader
        title="Codex 插件管理"
        description="管理本机 Codex 的本地插件市场与插件启用状态。"
      />

      {invalidMarketplaces.length > 0 ? (
        <Alert variant="warning">
          <AlertDescription>
            有 {invalidMarketplaces.length} 个插件市场 manifest
            无效或路径不可用， 建议先移除后重新注册。
          </AlertDescription>
        </Alert>
      ) : null}

      <AnimatedCard
        title="注册本地插件市场"
        description="source 必须包含 .agents/plugins/marketplace.json 或 .claude-plugin/marketplace.json。"
      >
        <div className="grid gap-3 md:grid-cols-[180px_1fr_auto] md:items-end">
          <div className="space-y-1.5">
            <Label htmlFor="marketplace-name">市场名</Label>
            <Input
              id="marketplace-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="team-curated"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="marketplace-source">本地路径</Label>
            <Input
              id="marketplace-source"
              value={source}
              onChange={(event) => setSource(event.target.value)}
              placeholder="C:/Users/me/.codex/.tmp/plugins-remote"
            />
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={defaultEnabled}
                onChange={(event) => setDefaultEnabled(event.target.checked)}
              />
              注册后启用
            </label>
            <Button
              type="button"
              disabled={register.isPending || !name.trim() || !source.trim()}
              onClick={() =>
                register.mutate({
                  name: name.trim(),
                  source: source.trim(),
                  default_enabled: defaultEnabled,
                })
              }
            >
              {register.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : null}
              注册
            </Button>
          </div>
        </div>
      </AnimatedCard>

      <AnimatedCard
        title="插件市场"
        description="marketplace 注册项会写入 Codex config.toml，不会删除本地文件。"
      >
        {query.isLoading ? (
          <div className="text-sm text-muted-foreground">加载中…</div>
        ) : query.isError ? (
          <Alert variant="error">
            <AlertDescription>Codex 插件配置加载失败。</AlertDescription>
          </Alert>
        ) : data?.marketplaces.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>市场</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>路径</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.marketplaces.map((marketplace) => (
                <TableRow key={marketplace.name}>
                  <TableCell className="font-medium">
                    {marketplace.name}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={
                        marketplace.manifest_valid ? "success" : "warning"
                      }
                    >
                      {marketplace.manifest_valid ? "有效" : "无效"}
                    </Badge>
                  </TableCell>
                  <TableCell className="max-w-[360px] truncate text-muted-foreground">
                    {marketplace.source ?? "—"}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={remove.isPending}
                      onClick={() => remove.mutate(marketplace.name)}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <div className="text-sm text-muted-foreground">
            尚未注册任何本地插件市场。
          </div>
        )}
      </AnimatedCard>

      <AnimatedCard
        title="插件启用状态"
        description="plugin_id 的格式为 plugin-name@marketplace-name。"
      >
        {data?.plugins.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>插件</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.plugins.map((plugin) => (
                <TableRow key={plugin.plugin_id}>
                  <TableCell className="font-medium">
                    {plugin.plugin_id}
                  </TableCell>
                  <TableCell>
                    <Badge variant={plugin.enabled ? "success" : "secondary"}>
                      {plugin.enabled ? "启用" : "禁用"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={toggle.isPending}
                      onClick={() =>
                        toggle.mutate({
                          plugin_id: plugin.plugin_id,
                          enabled: !plugin.enabled,
                        })
                      }
                    >
                      {toggle.isPending &&
                      toggle.variables?.plugin_id === plugin.plugin_id ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : null}
                      {plugin.enabled ? "禁用" : "启用"}
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <div className="text-sm text-muted-foreground">
            config.toml 中还没有插件配置。
          </div>
        )}
      </AnimatedCard>

      {data?.config_path ? (
        <p className="text-xs text-muted-foreground">
          配置文件：{data.config_path}
        </p>
      ) : null}
    </section>
  )
}
