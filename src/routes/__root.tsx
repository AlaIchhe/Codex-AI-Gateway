import { createRootRoute } from "@tanstack/react-router"

import { DashboardShell } from "@/components/dashboard-shell"

export const Route = createRootRoute({
  component: () => <DashboardShell />,
})
