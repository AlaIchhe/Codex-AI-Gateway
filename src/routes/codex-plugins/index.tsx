import { createFileRoute } from "@tanstack/react-router"

import { CodexPluginsSubPage } from "@/features/codex-plugins/CodexPluginsSubPage"

export const Route = createFileRoute("/codex-plugins/")({
  component: CodexPluginsSubPage,
})
