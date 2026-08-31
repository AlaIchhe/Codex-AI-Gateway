import type { ReactNode } from "react"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/coss/components/card"
import { BlurFade } from "@/components/magicui/blur-fade"

export function AnimatedCard({
  title,
  description,
  children,
  delay = 0,
  contentClassName,
  className,
}: {
  title: string
  description?: string
  children: ReactNode
  delay?: number
  contentClassName?: string
  className?: string
}) {
  return (
    <BlurFade className={className} delay={delay}>
      <Card className="relative h-full overflow-hidden transition-all duration-300 hover:-translate-y-1 hover:shadow-lg">
        <CardHeader>
          <CardTitle>{title}</CardTitle>
          {description ? (
            <CardDescription>{description}</CardDescription>
          ) : null}
        </CardHeader>
        <CardContent className={contentClassName}>{children}</CardContent>
      </Card>
    </BlurFade>
  )
}
