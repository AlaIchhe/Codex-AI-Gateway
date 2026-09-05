import { Switch as SwitchPrimitive } from "@base-ui/react/switch"
import type * as React from "react"
import { cn } from "@/lib/utils"

export function Switch({
  className,
  pending = false,
  ...props
}: SwitchPrimitive.Root.Props & { pending?: boolean }): React.ReactElement {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent bg-input transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50 data-[checked]:bg-primary",
        className,
      )}
      data-slot="switch"
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "pointer-events-none block size-4 translate-x-0.5 rounded-full bg-background shadow-sm transition-transform data-[checked]:translate-x-[18px]",
          pending && "animate-pulse",
        )}
        data-slot="switch-thumb"
      />
    </SwitchPrimitive.Root>
  )
}
