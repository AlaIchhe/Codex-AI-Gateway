import { createFileRoute } from "@tanstack/react-router"

import { CodexMcpSubPage } from "@/features/codex-plugins/CodexMcpSubPage"

export const Route = createFileRoute("/codex-plugins/mcp")({
  component: CodexMcpSubPage,
})
