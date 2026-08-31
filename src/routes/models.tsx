import { createFileRoute } from "@tanstack/react-router"
import { ModelsPage } from "@/features/models/ModelsPage"

const RoutePending = () => (
  <div className="flex min-h-[50vh] items-center justify-center">
    <div className="size-6 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
  </div>
)

export const Route = createFileRoute("/models")({
  pendingComponent: RoutePending,
  component: ModelsPage,
})
