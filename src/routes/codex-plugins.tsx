import { createFileRoute } from "@tanstack/react-router"
import { CodexPluginsPage } from "@/features/codex-plugins/CodexPluginsPage"

const RoutePending = () => (
  <div className="flex min-h-[50vh] items-center justify-center">
    <div className="size-6 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
  </div>
)

export const Route = createFileRoute("/codex-plugins")({
  pendingComponent: RoutePending,
  component: CodexPluginsPage,
})
