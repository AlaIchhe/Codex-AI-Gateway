import { z } from "zod"

export type Upstream = {
  id: string
  name: string
  status: "enabled" | "disabled"
  kind: "custom" | "preset"
  preset_id?: string | null
  preset_version?: string | null
  base_url: string
  last_health_result: string | null
  connectivity_probe: Record<string, unknown> | null
  protocol_probe_summary?: string
  preset_discovery?: PresetDiscoverySummary
}

export type PresetDiscoverySummary = {
  status: "never" | "succeeded" | "failed"
  current_model_count: number
  latest_snapshot_id: string | null
  latest_failure_id: string | null
  last_attempt_at: string | null
  last_success_at: string | null
  last_failure_at: string | null
}

export type PresetProvider = {
  preset_id: string
  name: string
  icon: string
  base_url: string
  doc_url: string
  model_source: string
  extractor_key: string
  extractor_version: string
  model_count: number | null
  current_model_count: number
  discovery_status: "never" | "succeeded" | "failed"
  source: Record<string, unknown>
}

export type PresetDiscovery = PresetDiscoverySummary & {
  upstream_id: string
  preset_id: string
  snapshots: Array<Record<string, unknown>>
  failures: Array<Record<string, unknown>>
}

export type UpstreamOffering = {
  id: string
  provider_model_id: string
  display_name: string
  status: string
  canonical_model_id: string | null
}

export type ModelSummary = {
  id: string
  display_name: string
  slug: string
  metadata_status: string
  source: string
  upstream_count: number
  upstream_names: string[]
  priority_summary: string
}

export type ModelDetail = {
  model: {
    id: string
    display_name: string
    openrouter_model_id: string | null
  }
  identity_evidence: Array<Record<string, unknown>>
  catalog_candidates: Array<Record<string, unknown>>
  catalog_evidence: Array<Record<string, unknown>>
}

export type RoutingPreference = {
  id: string
  scope: "global" | "canonical_model"
  canonical_model_id: string | null
  ordered_upstream_ids: string[]
  updated_at: string
}

export type GatewayTokenView = {
  id: string
  status: string
  prefix: string
  last4: string
  issued_at: string
  token?: string | null
}

export type UsageSummaryRow = {
  bucket_start: string
  attempts: number
  provider_reported_input_tokens: number
  estimated_output_tokens: number
  cost_minor_units: number
  currency: string
  reasoning_tokens: number
  cache_read_tokens: number
}

export type UsageAttempt = {
  id: string
  started_at: string
  duration_ms: number | null
  outcome: string
  reporting_basis: string
  outbound_protocol: string | null
  upstream_label?: string | null
  canonical_model_label?: string | null
  attempt_ordinal?: number
  fallback_trigger?: string | null
  error_mapping_code?: string | null
  tokens: Record<string, number>
}

export const upstreamCreateSchema = z.object({
  name: z
    .string()
    .trim()
    .min(1, "请输入上游名称。")
    .max(80, "上游名称不能超过 80 个字符。"),
  base_url: z.string().trim().url("请输入合法的 http(s) Base URL。"),
  api_credential: z.string().min(1, "请输入 API 凭据。"),
})

export const presetCreateSchema = z.object({
  preset_id: z.string().min(1, "请选择预设 Provider。"),
  api_credential: z.string().min(1, "请输入 API 凭据。"),
})

export const upstreamUpdateSchema = z.object({
  name: z
    .string()
    .trim()
    .min(1, "请输入上游名称。")
    .max(80, "上游名称不能超过 80 个字符。"),
  base_url: z.string().trim().url("请输入合法的 http(s) Base URL。"),
  api_credential: z.string().optional(),
})

export type UpstreamCreateValues = z.infer<typeof upstreamCreateSchema>
export type PresetCreateValues = z.infer<typeof presetCreateSchema>
export type UpstreamUpdateValues = z.infer<typeof upstreamUpdateSchema>
export type UpstreamCreatePayload =
  | (UpstreamCreateValues & { kind?: "custom" })
  | (PresetCreateValues & { kind: "preset" })

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  })
  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok)
    throw new Error(
      data?.detail || data?.error?.message || "请求失败，请稍后重试。",
    )
  return data as T
}

export const api = {
  listUpstreams: () => request<Upstream[]>("/admin/upstreams"),
  listPresets: () => request<PresetProvider[]>("/admin/presets"),
  createUpstream: (body: UpstreamCreatePayload) =>
    request<Upstream>("/admin/upstreams", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateUpstream: (id: string, body: UpstreamUpdateValues) =>
    request<Upstream>(`/admin/upstreams/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  updateUpstreamStatus: (id: string, status: "enabled" | "disabled") =>
    request<Upstream>(`/admin/upstreams/${id}`, {
      method: "PUT",
      body: JSON.stringify({ status }),
    }),
  deleteUpstream: (id: string) =>
    request<{ id: string }>(`/admin/upstreams/${id}`, { method: "DELETE" }),
  probeUpstream: (id: string) =>
    request<Upstream>(`/admin/upstreams/${id}/probe`, { method: "POST" }),
  getPresetDiscovery: (id: string) =>
    request<PresetDiscovery>(`/admin/upstreams/${id}/discovery`),
  listUpstreamOfferings: (id: string) =>
    request<UpstreamOffering[]>(`/admin/upstreams/${id}/offerings`),
  listModels: () => request<ModelSummary[]>("/admin/models"),
  getModel: (id: string) => request<ModelDetail>(`/admin/models/${id}`),
  listRouting: () => request<RoutingPreference[]>("/admin/routing"),
  putGlobalRouting: (upstreamIds: string[]) =>
    request<unknown>("/admin/routing/global", {
      method: "PUT",
      body: JSON.stringify({ ordered_upstream_ids: upstreamIds }),
    }),
  putModelRouting: (id: string, upstreamIds: string[]) =>
    request<unknown>(`/admin/routing/models/${id}`, {
      method: "PUT",
      body: JSON.stringify({ ordered_upstream_ids: upstreamIds }),
    }),
  getGatewayToken: () => request<GatewayTokenView>("/admin/gateway-token"),
  rotateGatewayToken: () =>
    request<GatewayTokenView>("/admin/gateway-token/rotate", {
      method: "POST",
    }),
  listUsageSummary: (groupBy = "period") =>
    request<{ rows: UsageSummaryRow[] }>(
      `/admin/usage/summary?group_by=${groupBy}`,
    ),
  listUsageAttempts: (params?: { limit?: number; before?: string }) => {
    const search = new URLSearchParams()
    if (params?.limit) search.set("limit", String(params.limit))
    if (params?.before) search.set("before", params.before)
    const qs = search.toString()
    return request<{ items: UsageAttempt[]; next_cursor: string | null }>(
      `/admin/usage/attempts${qs ? `?${qs}` : ""}`,
    )
  },
  updateRetention: (days: number) =>
    request<unknown>("/admin/settings", {
      method: "PATCH",
      body: JSON.stringify({ usage_retention_days: days }),
    }),
}
