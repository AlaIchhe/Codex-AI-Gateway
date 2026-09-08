import { createFileRoute } from "@tanstack/react-router"

import { UpdatePage } from "@/features/update/UpdatePage"

const RoutePending = () => (
  <div className="flex min-h-[50vh] items-center justify-center">
    <div className="size-6 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
  </div>
)

export const Route = createFileRoute("/update")({
  pendingComponent: RoutePending,
  component: UpdatePage,
})
