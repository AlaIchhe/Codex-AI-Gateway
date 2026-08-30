import { createFileRoute } from "@tanstack/react-router"
import { UpstreamsPage } from "@/features/upstreams/UpstreamsPage"
export const Route = createFileRoute("/upstreams")({ component: UpstreamsPage })
