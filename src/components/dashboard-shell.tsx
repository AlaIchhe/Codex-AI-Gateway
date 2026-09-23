import { Link, Outlet } from "@tanstack/react-router"
import {
  Blocks,
  Boxes,
  ChartColumn,
  CloudDownload,
  LayoutGrid,
  Menu,
  Monitor,
  Moon,
  Server,
  Sun,
  Waypoints,
  X,
} from "lucide-react"
import { useState } from "react"
import { Button } from "@/components/coss/components/button"
import { useTheme } from "@/components/theme-provider"
import { cn } from "@/lib/utils"

const navigation = [
  { to: "/", label: "总览", icon: LayoutGrid },
  { to: "/upstreams", label: "上游", icon: Server },
  { to: "/models", label: "模型", icon: Boxes },
  { to: "/codex-plugins", label: "插件", icon: Blocks },
  { to: "/usage", label: "用量", icon: ChartColumn },
  { to: "/update", label: "更新", icon: CloudDownload },
] as const

const THEME_ORDER = ["light", "dark", "system"] as const
const THEME_META = {
  light: { label: "浅色", icon: Sun },
  dark: { label: "深色", icon: Moon },
  system: { label: "跟随系统", icon: Monitor },
} as const

function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const meta = THEME_META[theme]
  const Icon = meta.icon
  const next =
    THEME_ORDER[(THEME_ORDER.indexOf(theme) + 1) % THEME_ORDER.length]
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-label={`主题：${meta.label}，切换为${THEME_META[next].label}`}
      title={`主题：${meta.label}（按 D 切换）`}
      onClick={() => setTheme(next)}
      className="text-muted-foreground hover:text-foreground"
    >
      <Icon className="size-4" />
    </Button>
  )
}

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="管理导航" className="grid gap-px">
      {navigation.map(({ to, label, icon: Icon }) => (
        <Link
          key={to}
          to={to}
          onClick={onNavigate}
          activeOptions={{ exact: to === "/" }}
          className="flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] text-muted-foreground transition-colors hover:bg-sidebar-accent/60 hover:text-foreground data-[status=active]:bg-sidebar-accent data-[status=active]:font-medium data-[status=active]:text-foreground"
        >
          <Icon className="size-4 shrink-0" strokeWidth={1.75} />
          {label}
        </Link>
      ))}
    </nav>
  )
}

function Brand() {
  return (
    <Link to="/" className="flex items-center gap-2 px-2">
      <span className="grid size-6 place-items-center rounded-md bg-foreground text-background">
        <Waypoints className="size-3.5" strokeWidth={2.25} />
      </span>
      <span className="text-sm font-semibold tracking-tight">
        Codex Gateway
      </span>
    </Link>
  )
}

function TrustNote({ className }: { className?: string }) {
  return (
    <p
      className={cn(
        "flex items-start gap-2 px-2 text-xs leading-relaxed text-muted-foreground",
        className,
      )}
    >
      <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-warning" />
      无鉴权控制模式，请仅在可信网络内使用。
    </p>
  )
}

export function DashboardShell() {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="flex min-h-dvh bg-background text-foreground">
      <aside className="sticky top-0 hidden h-dvh w-56 shrink-0 flex-col gap-6 border-r border-sidebar-border bg-sidebar px-3 py-4 lg:flex">
        <div className="flex items-center justify-between">
          <Brand />
          <ThemeToggle />
        </div>
        <NavLinks />
        <TrustNote className="mt-auto" />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex h-12 items-center justify-between border-b bg-background/80 px-3 backdrop-blur-md lg:hidden">
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={mobileOpen ? "关闭导航" : "打开导航"}
              aria-expanded={mobileOpen}
              onClick={() => setMobileOpen((open) => !open)}
            >
              {mobileOpen ? (
                <X className="size-4" />
              ) : (
                <Menu className="size-4" />
              )}
            </Button>
            <Brand />
          </div>
          <ThemeToggle />
        </header>

        {mobileOpen ? (
          <div className="grid gap-4 border-b bg-sidebar px-3 py-3 lg:hidden">
            <NavLinks onNavigate={() => setMobileOpen(false)} />
            <TrustNote />
          </div>
        ) : null}

        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-8 lg:py-10">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
