import { createFileRoute } from "@tanstack/react-router"

import { CodexSkillsSubPage } from "@/features/codex-plugins/CodexSkillsSubPage"

export const Route = createFileRoute("/codex-plugins/skills")({
  component: CodexSkillsSubPage,
})
