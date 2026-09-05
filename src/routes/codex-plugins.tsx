import { createFileRoute, Outlet } from "@tanstack/react-router"

import { CodexPluginsNav } from "@/features/codex-plugins/CodexPluginsNav"

const RoutePending = () => (
  <div className="flex min-h-[50vh] items-center justify-center">
    <div className="size-6 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
  </div>
)

function CodexPluginsLayout() {
  return (
    <section aria-label="Codex 上下文" className="space-y-4">
      <CodexPluginsNav />
      <Outlet />
    </section>
  )
}

export const Route = createFileRoute("/codex-plugins")({
  pendingComponent: RoutePending,
  component: CodexPluginsLayout,
})
