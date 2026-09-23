import { createFileRoute } from "@tanstack/react-router"

import { UpdatePage } from "@/features/update/UpdatePage"

export const Route = createFileRoute("/update")({
  component: UpdatePage,
})
