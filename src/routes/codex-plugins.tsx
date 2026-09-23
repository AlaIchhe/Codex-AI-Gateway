import { createFileRoute, Outlet } from "@tanstack/react-router"

import { CodexPluginsNav } from "@/features/codex-plugins/CodexPluginsNav"

function CodexPluginsLayout() {
  return (
    <section aria-label="Codex 上下文" className="space-y-6">
      <CodexPluginsNav />
      <Outlet />
    </section>
  )
}

export const Route = createFileRoute("/codex-plugins")({
  component: CodexPluginsLayout,
})
