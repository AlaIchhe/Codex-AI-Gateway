import { createRouter } from "@tanstack/react-router"
import { routeTree } from "./routeTree.gen"

function RouteError({ error }: { error: Error }) {
  return (
    <div className="flex min-h-[50vh] flex-col items-center justify-center gap-3">
      <p className="text-sm font-medium">页面渲染出现错误</p>
      <p className="max-w-md text-center text-sm text-muted-foreground">
        {error.message || "发生了未知错误，请重试。"}
      </p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="mt-1 h-8 rounded-lg border px-3 text-sm font-medium transition-colors hover:bg-accent"
      >
        重新加载
      </button>
    </div>
  )
}

function RoutePending() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <div className="size-3.5 animate-spin rounded-full border-[1.5px] border-current border-t-transparent" />
        加载中…
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
