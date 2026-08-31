import { createRouter } from "@tanstack/react-router"
import { routeTree } from "./routeTree.gen"

function RouteError({ error }: { error: Error }) {
  return (
    <div className="flex min-h-[50vh] flex-col items-center justify-center gap-4">
      <p className="text-lg font-medium text-destructive">页面渲染出现错误</p>
      <p className="max-w-md text-center text-sm text-muted-foreground">
        {error.message || "发生了未知错误，请重试。"}
      </p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="rounded-md border px-4 py-2 text-sm font-medium transition-colors hover:bg-muted"
      >
        重新加载
      </button>
    </div>
  )
}

function RoutePending() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center">
      <div className="flex flex-col items-center gap-3">
        <div className="size-6 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
        <p className="text-sm text-muted-foreground">加载中…</p>
      </div>
    </div>
  )
}

export const router = createRouter({
  routeTree,
  defaultErrorComponent: RouteError,
  defaultPendingComponent: RoutePending,
})
declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}
