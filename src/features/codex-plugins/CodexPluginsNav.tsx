import { Link } from "@tanstack/react-router"

import {
  segmentedControlItemVariants,
  segmentedControlRootClassName,
} from "@/lib/segmented-control"

const TABS = [
  { to: "/codex-plugins", label: "插件", exact: true },
  { to: "/codex-plugins/mcp", label: "MCP", exact: true },
  { to: "/codex-plugins/skills", label: "技能", exact: true },
] as const

/** 插件/MCP/技能 子页切换：COSS segmented-control（Navigation links + aria-current）。 */
export function CodexPluginsNav() {
  return (
    <nav
      aria-label="Codex 上下文子页"
      className={segmentedControlRootClassName}
    >
      {TABS.map((tab) => (
        <Link
          key={tab.to}
          to={tab.to}
          activeOptions={{ exact: tab.exact }}
          activeProps={{ "aria-current": "page" as const }}
          className={segmentedControlItemVariants({ state: "current" })}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  )
}
