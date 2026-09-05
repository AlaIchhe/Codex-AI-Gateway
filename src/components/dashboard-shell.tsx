import { Link, Outlet } from "@tanstack/react-router"
import {
  BarChart3,
  Boxes,
  Menu,
  Network,
  Package,
  ShieldAlert,
  X,
} from "lucide-react"
import { useState } from "react"
import { Button } from "@/components/coss/components/button"
import { AnimatedGridPattern } from "@/components/magicui/animated-grid-pattern"
import { AnimatedShinyText } from "@/components/magicui/animated-shiny-text"
import { AnimatedThemeToggler } from "@/components/magicui/animated-theme-toggler"
import { LightRays } from "@/components/magicui/light-rays"
import { cn } from "@/lib/utils"

const navigation = [
  { to: "/", label: "总览", icon: Boxes },
  { to: "/upstreams", label: "上游", icon: Network },
  { to: "/models", label: "模型", icon: ShieldAlert },
  { to: "/codex-plugins", label: "插件", icon: Package },
  { to: "/usage", label: "用量", icon: BarChart3 },
] as const

function NavLinks({
  className,
  onNavigate,
}: {
  className?: string
  onNavigate?: () => void
}) {
  return (
    <nav aria-label="管理导航" className={cn("grid gap-1", className)}>
      {navigation.map((item) => {
        const Icon = item.icon
        return (
          <Link
            key={item.to}
            to={item.to}
            onClick={onNavigate}
            activeProps={{
              className:
                "border-primary/40 bg-primary/10 text-primary shadow-[inset_0_1px_0_0_rgba(255,255,255,0.08)]",
            }}
            className="group relative flex items-center gap-3 rounded-xl border border-transparent px-3 py-2 text-sm text-muted-foreground transition-all duration-300 hover:border-border hover:bg-muted/70 hover:text-foreground"
          >
            <span className="absolute inset-y-2 left-0 w-px rounded-full bg-transparent transition-colors group-data-[status=active]:bg-primary" />
            <Icon className="size-4 transition-transform duration-300 group-hover:-translate-y-0.5 group-hover:scale-105" />
            <span>{item.label}</span>
          </Link>
        )
      })}
    </nav>
  )
}

export function DashboardShell() {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="relative min-h-dvh bg-background text-foreground">
      <LightRays
        count={5}
        color="rgba(140, 180, 255, 0.12)"
        blur={28}
        speed={18}
        className="fixed inset-0 -z-10"
      />
      <AnimatedGridPattern
        numSquares={40}
        maxOpacity={0.08}
        className="fixed inset-0 -z-10 [mask-image:radial-gradient(ellipse_at_center,white,transparent_72%)]"
      />

      <div className="flex min-h-dvh">
        <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 flex-col overflow-hidden border-r bg-card/65 p-4  lg:flex">
          <Link to="/" className="mb-6 flex items-center gap-3 rounded-xl p-2">
            <span className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground shadow-lg">
              <Network className="size-5" />
            </span>
            <span className="font-heading text-base font-semibold tracking-tight">
              Codex AI Gateway
            </span>
          </Link>

          <NavLinks />

          <div className="mt-auto rounded-xl border bg-background/70 p-3">
            <p className="text-xs font-medium text-foreground">
              无鉴权控制模式
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              请仅在可信网络内使用。
            </p>
          </div>
        </aside>

        <div className="relative flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-40 border-b bg-background/82 ">
            <div className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-3 px-4">
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={mobileOpen ? "关闭导航" : "打开导航"}
                  className="lg:hidden"
                  onClick={() => setMobileOpen((open) => !open)}
                >
                  {mobileOpen ? (
                    <X className="size-4" />
                  ) : (
                    <Menu className="size-4" />
                  )}
                </Button>
                <Link
                  to="/"
                  className="flex items-center gap-2 font-semibold lg:hidden"
                >
                  <Network className="size-4" />
                  Gateway
                </Link>
                <AnimatedShinyText className="hidden text-sm sm:block">
                  管理 · 路由 · 用量审计
                </AnimatedShinyText>
              </div>
              <AnimatedThemeToggler className="grid size-9 place-items-center rounded-full border text-foreground/80 transition-colors hover:bg-muted hover:text-foreground [&_svg]:size-4" />
            </div>
          </header>

          {mobileOpen ? (
            <div className="border-b bg-background/92 px-4 py-3  lg:hidden">
              <NavLinks onNavigate={() => setMobileOpen(false)} />
            </div>
          ) : null}

          <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
            <Outlet />
          </main>

          <footer className="border-t bg-background/70 px-4 py-3 text-center text-xs text-muted-foreground ">
            无鉴权控制模式：管理端有意不设登录，请在可信网络内使用。
          </footer>
        </div>
      </div>
    </div>
  )
}
