import type { LucideIcon } from "lucide-react"
import { cn } from "@/lib/utils"

export const ICON_STROKE_WIDTH = {
  inactive: 1.75,
  active: 2.5,
} as const

export type IconState = "inactive" | "active"

type StateIconProps = {
  icon: LucideIcon
  state?: IconState
  label: string
  className?: string
}

/**
 * 业务界面唯一允许设置 lucide strokeWidth 的入口。
 * 激活态同时使用加粗线宽、字重和下划线，不依赖颜色作为唯一信号。
 */
export function StateIcon({
  icon: Icon,
  state = "inactive",
  label,
  className,
}: StateIconProps) {
  return (
    <span
      className={cn("inline-flex", className)}
      role="img"
      aria-label={label}
    >
      <Icon aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH[state]} />
    </span>
  )
}
