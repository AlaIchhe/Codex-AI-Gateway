import type { ReactNode } from "react"
import { ResponsiveContainer } from "recharts"

export function ChartFrame({
  testId,
  label,
  children,
}: {
  testId: string
  label: string
  children: ReactNode
}) {
  return (
    <div
      className="h-72 w-full"
      data-testid={testId}
      role="img"
      aria-label={label}
    >
      <ResponsiveContainer width="100%" height="100%">
        {children}
      </ResponsiveContainer>
    </div>
  )
}
