import * as React from "react"
import { Switch as GlassSwitch } from "glisse"

import { cn } from "@/lib/utils"

function Switch({
  className,
  size = "default",
  ...props
}: React.ComponentProps<typeof GlassSwitch> & {
  size?: "sm" | "default"
}) {
  return (
    <GlassSwitch
      data-slot="switch"
      data-size={size}
      className={cn(
        "peer shrink-0 focus-visible:ring-3 focus-visible:ring-ring/50",
        className
      )}
      {...props}
    />
  )
}

export { Switch }
