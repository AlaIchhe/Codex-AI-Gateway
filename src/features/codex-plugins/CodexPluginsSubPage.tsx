import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ExternalLink, Loader2, Search, Trash2 } from "lucide-react"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { AnimatedCard } from "@/components/animated-card"
import { Alert, AlertDescription } from "@/components/coss/components/alert"
import { Badge } from "@/components/coss/components/badge"
import { Button } from "@/components/coss/components/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/coss/components/card"
import {
  Dialog,
  DialogDescription,
  DialogHeader,
  DialogPanel,
  DialogPopup,
  DialogTitle,
} from "@/components/coss/components/dialog"
import { Input } from "@/components/coss/components/input"
import { Label } from "@/components/coss/components/label"
import { Switch } from "@/components/coss/components/switch"
import {
  api,
  type CodexPluginCatalogEntry,
  type CodexPluginMarketplace,
} from "@/lib/api"

function PluginIcon({
  entry,
  className = "size-10",
}: {
  entry: CodexPluginCatalogEntry
  className?: string
}) {
  const [failed, setFailed] = useState(false)
  const label = entry.display_name ?? entry.name
  if (entry.icon_url && !failed) {
    return (
      <img
        src={entry.icon_url}
        alt=""
        loading="lazy"
        className={`${className} shrink-0 rounded-lg border bg-background object-contain p-1`}
        onError={() => setFailed(true)}
      />
    )
  }
  return (
    <div
      className={`${className} flex shrink-0 items-center justify-center rounded-lg border font-semibold text-white`}
      style={
        entry.brand_color
          ? { backgroundColor: entry.brand_color }
          : { backgroundColor: "var(--muted-foreground)" }
      }
      aria-hidden
    >
      {label.slice(0, 1).toUpperCase()}
    </div>
  )
}

function statusBadge(entry: CodexPluginCatalogEntry) {
  if (entry.stale) {
    return <Badge variant="warning">已失效</Badge>
  }
  if (entry.enabled) {
    return <Badge variant="success">已启用</Badge>
  }
  if (entry.configured) {
    return <Badge variant="secondary">已禁用</Badge>
  }
  return <Badge variant="outline">未配置</Badge>
}

function PluginCard({
  entry,
  togglePendingId,
  onToggle,
  onShowDetail,
}: {
  entry: CodexPluginCatalogEntry
  togglePendingId: string | null
  onToggle: (entry: CodexPluginCatalogEntry, enabled: boolean) => void
  onShowDetail: (entry: CodexPluginCatalogEntry) => void
}) {
  const pending = togglePendingId === entry.plugin_id
  const title = entry.display_name ?? entry.name
  const description = entry.description ?? entry.long_description
  return (
    <div className="flex flex-col gap-2.5 rounded-xl border bg-card p-4 transition-shadow hover:shadow-md">
      <div className="flex items-start gap-3">
        <PluginIcon entry={entry} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{title}</span>
            {entry.version ? (
              <span className="shrink-0 text-xs text-muted-foreground">
                v{entry.version}
              </span>
            ) : null}
          </div>
          <div
            className="truncate text-xs text-muted-foreground"
            title={entry.plugin_id}
          >
            {entry.plugin_id}
          </div>
        </div>
        {entry.stale ? (
          entry.enabled ? (
            <Button
              size="xs"
              variant="destructive-outline"
              disabled={pending}
              onClick={() => onToggle(entry, false)}
            >
              禁用
            </Button>
          ) : (
            statusBadge(entry)
          )
        ) : (
          <Switch
            checked={entry.enabled}
            disabled={pending}
            pending={pending}
            aria-label={`${entry.enabled ? "禁用" : "启用"} ${title}`}
            onCheckedChange={(checked) => onToggle(entry, checked)}
          />
        )}
      </div>
      <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">
        {description ?? "暂无描述。"}
      </p>
      <div className="flex flex-wrap items-center gap-1.5">
        {entry.category ? (
          <Badge variant="secondary">{entry.category}</Badge>
        ) : null}
        {entry.capabilities.slice(0, 3).map((capability) => (
          <Badge key={capability} variant="outline">
            {capability}
          </Badge>
        ))}
        {entry.stale ? (
          <span className="text-xs text-muted-foreground">
            插件已不在市场 manifest 中
          </span>
        ) : null}
        <Button
          size="xs"
          variant="ghost"
          className="ml-auto"
          onClick={() => onShowDetail(entry)}
        >
          详情
        </Button>
      </div>
    </div>
  )
}

function PluginDetailDialog({
  entry,
  onClose,
}: {
  entry: CodexPluginCatalogEntry | null
  onClose: () => void
}) {
  if (!entry) {
    return null
  }
  const title = entry.display_name ?? entry.name
  const author = entry.author ?? entry.developer_name
  const longDescription = entry.long_description ?? entry.description
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogPopup className="sm:max-w-xl">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <PluginIcon entry={entry} className="size-12" />
            <div className="min-w-0">
              <DialogTitle>{title}</DialogTitle>
              <DialogDescription className="truncate">
                {entry.plugin_id}
                {entry.version ? ` · v${entry.version}` : ""}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>
        <DialogPanel className="space-y-4 overflow-y-auto">
          {longDescription ? (
            <p className="text-sm leading-relaxed">{longDescription}</p>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            {statusBadge(entry)}
            {entry.category ? (
              <Badge variant="secondary">{entry.category}</Badge>
            ) : null}
            {entry.capabilities.map((capability) => (
              <Badge key={capability} variant="outline">
                {capability}
              </Badge>
            ))}
          </div>
          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[96px_1fr]">
            {author ? (
              <>
                <dt className="text-muted-foreground">作者</dt>
                <dd>{author}</dd>
              </>
            ) : null}
            {entry.keywords.length ? (
              <>
                <dt className="text-muted-foreground">关键词</dt>
                <dd>{entry.keywords.join("、")}</dd>
              </>
            ) : null}
            {entry.homepage || entry.repository ? (
              <>
                <dt className="text-muted-foreground">链接</dt>
                <dd className="flex flex-wrap gap-x-4 gap-y-1">
                  {entry.homepage ? (
                    <a
                      href={entry.homepage}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-primary hover:underline"
                    >
                      主页 <ExternalLink className="size-3" />
                    </a>
                  ) : null}
                  {entry.repository ? (
                    <a
                      href={entry.repository}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-primary hover:underline"
                    >
                      仓库 <ExternalLink className="size-3" />
                    </a>
                  ) : null}
                </dd>
              </>
            ) : null}
            {!entry.has_metadata ? (
              <>
                <dt className="text-muted-foreground">元数据</dt>
                <dd className="text-muted-foreground">
                  未能读取插件目录下的 .codex-plugin/plugin.json。
                </dd>
              </>
            ) : null}
          </dl>
        </DialogPanel>
      </DialogPopup>
    </Dialog>
  )
}

function MarketplaceSection({
  marketplace,
  togglePendingId,
  onToggle,
  onShowDetail,
  onRemove,
  removePending,
}: {
  marketplace: CodexPluginMarketplace
  togglePendingId: string | null
  onToggle: (entry: CodexPluginCatalogEntry, enabled: boolean) => void
  onShowDetail: (entry: CodexPluginCatalogEntry) => void
  onRemove: (name: string) => void
  removePending: boolean
}) {
  const enabledCount = marketplace.catalog.filter(
    (entry) => entry.enabled && !entry.stale,
  ).length
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {marketplace.name}
          <Badge variant={marketplace.manifest_valid ? "success" : "warning"}>
            {marketplace.manifest_valid ? "有效" : "无效"}
          </Badge>
        </CardTitle>
        <CardDescription
          className="max-w-xl truncate"
          title={marketplace.source ?? undefined}
        >
          {marketplace.source ?? "未配置路径"} · 已启用 {enabledCount} / 共{" "}
          {marketplace.catalog.length}
        </CardDescription>
        <CardAction>
          <Button
            size="icon-sm"
            variant="ghost"
            disabled={removePending}
            aria-label={`移除市场 ${marketplace.name}`}
            onClick={() => onRemove(marketplace.name)}
          >
            <Trash2 className="size-4" />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-3">
        {!marketplace.manifest_valid ? (
          marketplace.source_type === "git" ? (
            <Alert variant="info">
              <AlertDescription>
                git 市场：manifest 需由 Codex 拉取到本地缓存（
                ~/.codex/.tmp/marketplaces/{marketplace.name}
                ）后才能浏览；不会影响 Codex 内的正常使用。
              </AlertDescription>
            </Alert>
          ) : (
            <Alert variant="warning">
              <AlertDescription>
                market manifest 无效或路径不可用，建议移除后重新注册。
              </AlertDescription>
            </Alert>
          )
        ) : marketplace.catalog.length ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {marketplace.catalog.map((entry) => (
              <PluginCard
                key={entry.plugin_id}
                entry={entry}
                togglePendingId={togglePendingId}
                onToggle={onToggle}
                onShowDetail={onShowDetail}
              />
            ))}
          </div>
        ) : (
          <div className="text-sm text-muted-foreground">该市场暂无插件。</div>
        )}
      </CardContent>
    </Card>
  )
}

export function CodexPluginsSubPage() {
  const queryClient = useQueryClient()
  const [name, setName] = useState("")
  const [source, setSource] = useState("")
  const [defaultEnabled, setDefaultEnabled] = useState(false)
  const [search, setSearch] = useState("")
  const [category, setCategory] = useState<string | null>(null)
  const [detail, setDetail] = useState<CodexPluginCatalogEntry | null>(null)

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
  const marketplaces = data?.marketplaces ?? []

  const categories = useMemo(() => {
    const counts = new Map<string, number>()
    for (const marketplace of marketplaces) {
      for (const entry of marketplace.catalog) {
        if (entry.category) {
          counts.set(entry.category, (counts.get(entry.category) ?? 0) + 1)
        }
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [marketplaces])

  const keyword = search.trim().toLowerCase()
  const matches = (entry: CodexPluginCatalogEntry) => {
    if (category && entry.category !== category) {
      return false
    }
    if (!keyword) {
      return true
    }
    const haystack = [
      entry.display_name,
      entry.name,
      entry.description,
      entry.long_description,
      entry.plugin_id,
      ...entry.keywords,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
    return haystack.includes(keyword)
  }

  const orphans =
    data?.plugins.filter((plugin) => !plugin.marketplace_registered) ?? []

  return (
    <>
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

      {query.isLoading ? (
        <div className="text-sm text-muted-foreground">加载中…</div>
      ) : query.isError ? (
        <Alert variant="error">
          <AlertDescription>Codex 插件配置加载失败。</AlertDescription>
        </Alert>
      ) : marketplaces.length ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-56 flex-1 sm:max-w-xs">
              <Search className="pointer-events-none absolute start-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索插件名称、描述或关键词…"
                className="ps-8"
                aria-label="搜索插件"
              />
            </div>
            {categories.length ? (
              <div className="flex flex-wrap items-center gap-1.5">
                <Button
                  size="xs"
                  variant={category === null ? "default" : "outline"}
                  onClick={() => setCategory(null)}
                >
                  全部
                </Button>
                {categories.map(([item, count]) => (
                  <Button
                    key={item}
                    size="xs"
                    variant={category === item ? "default" : "outline"}
                    onClick={() => setCategory(category === item ? null : item)}
                  >
                    {item} ({count})
                  </Button>
                ))}
              </div>
            ) : null}
          </div>

          <div className="space-y-4">
            {marketplaces.map((marketplace) => {
              const visible = marketplace.catalog.filter(matches)
              return (
                <MarketplaceSection
                  key={marketplace.name}
                  marketplace={
                    visible.length === marketplace.catalog.length
                      ? marketplace
                      : { ...marketplace, catalog: visible }
                  }
                  togglePendingId={
                    toggle.isPending
                      ? (toggle.variables?.plugin_id ?? null)
                      : null
                  }
                  onToggle={(entry, enabled) =>
                    toggle.mutate({
                      plugin_id: entry.plugin_id,
                      enabled,
                    })
                  }
                  onShowDetail={setDetail}
                  onRemove={(marketName) => remove.mutate(marketName)}
                  removePending={remove.isPending}
                />
              )
            })}
          </div>

          {marketplaces.every(
            (marketplace) => marketplace.catalog.filter(matches).length === 0,
          ) ? (
            <div className="text-sm text-muted-foreground">
              没有匹配搜索条件的插件。
            </div>
          ) : null}
        </>
      ) : (
        <div className="text-sm text-muted-foreground">
          尚未注册任何本地插件市场。
        </div>
      )}

      {orphans.length ? (
        <AnimatedCard
          title="未关联市场的插件配置"
          description="以下插件存在于 config.toml，但其市场未注册，无法在界面切换。"
        >
          <ul className="space-y-1.5 text-sm">
            {orphans.map((plugin) => (
              <li key={plugin.plugin_id} className="flex items-center gap-2">
                <code className="text-xs">{plugin.plugin_id}</code>
                <Badge variant={plugin.enabled ? "success" : "secondary"}>
                  {plugin.enabled ? "启用" : "禁用"}
                </Badge>
              </li>
            ))}
          </ul>
        </AnimatedCard>
      ) : null}

      <PluginDetailDialog entry={detail} onClose={() => setDetail(null)} />

      {data?.config_path ? (
        <p className="text-xs text-muted-foreground">
          配置文件：{data.config_path}
        </p>
      ) : null}
    </>
  )
}
