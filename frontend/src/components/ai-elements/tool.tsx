import type { ComponentProps, ReactNode } from "react"
import {
  CheckCircleIcon,
  ChevronDownIcon,
  ClockIcon,
  WrenchIcon,
  XCircleIcon,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

export type ToolState = "running" | "completed" | "error"

const status: Record<
  ToolState,
  { label: string; icon: ReactNode; className?: string }
> = {
  running: {
    label: "Running",
    icon: <ClockIcon className="size-3.5 animate-pulse" />,
  },
  completed: {
    label: "Completed",
    icon: <CheckCircleIcon className="size-3.5 text-primary" />,
  },
  error: {
    label: "Error",
    icon: <XCircleIcon className="size-3.5 text-destructive" />,
    className: "text-destructive",
  },
}

function JsonValue({ value }: { value: unknown }) {
  if (value === undefined || value === null || value === "") {
    return null
  }
  const text =
    typeof value === "string" ? value : JSON.stringify(value, null, 2)
  return (
    <pre className="max-h-64 overflow-auto rounded-md bg-muted/60 p-3 font-mono text-xs leading-5 whitespace-pre-wrap">
      {text}
    </pre>
  )
}

export function Tool({
  className,
  ...props
}: ComponentProps<typeof Collapsible>) {
  return (
    <Collapsible
      className={cn("group/tool w-full rounded-lg border bg-background", className)}
      {...props}
    />
  )
}

export function ToolHeader({
  name,
  state,
}: {
  name: string
  state: ToolState
}) {
  const current = status[state]
  return (
    <CollapsibleTrigger className="flex w-full items-center justify-between gap-4 p-3 text-left">
      <span className="flex min-w-0 items-center gap-2">
        <WrenchIcon className="size-4 shrink-0 text-[var(--protocol)]" />
        <span className="truncate text-sm font-medium">{name}</span>
        <Badge
          variant="secondary"
          className={cn("gap-1.5 text-xs", current.className)}
        >
          {current.icon}
          {current.label}
        </Badge>
      </span>
      <ChevronDownIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]/tool:rotate-180" />
    </CollapsibleTrigger>
  )
}

export function ToolContent({
  className,
  ...props
}: ComponentProps<typeof CollapsibleContent>) {
  return (
    <CollapsibleContent
      className={cn("space-y-4 border-t p-3", className)}
      {...props}
    />
  )
}

export function ToolInput({ input }: { input: unknown }) {
  if (input === undefined || input === null) {
    return null
  }
  return (
    <section className="space-y-2">
      <h4 className="text-xs font-medium text-muted-foreground">Parameters</h4>
      <JsonValue value={input} />
    </section>
  )
}

export function ToolOutput({
  output,
  errorText,
}: {
  output?: unknown
  errorText?: string
}) {
  if (output === undefined && !errorText) {
    return null
  }
  return (
    <section className="space-y-2">
      <h4
        className={cn(
          "text-xs font-medium text-muted-foreground",
          errorText && "text-destructive"
        )}
      >
        {errorText ? "Error" : "Result"}
      </h4>
      <JsonValue value={errorText || output} />
    </section>
  )
}
