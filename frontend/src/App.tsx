import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react"
import {
  AlertCircle,
  ArrowUp,
  Bot,
  Boxes,
  ChevronDown,
  Database,
  KeyRound,
  LogOut,
  Moon,
  Network,
  ShieldCheck,
  Square,
  Sun,
  UserRound,
  Wrench,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupText,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import {
  Message,
  MessageAvatar,
  MessageContent,
  MessageHeader,
} from "@/components/ui/message"
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  Tool,
  ToolContent,
  ToolHeader,
  ToolInput,
  ToolOutput,
  type ToolState,
} from "@/components/ai-elements/tool"
import { cn } from "@/lib/utils"

const MarkdownResponse = lazy(() => import("@/components/markdown-response"))

type Theme = "light" | "dark"
type ChatRole = "user" | "assistant" | "error"

type ToolTrace = {
  id: string
  name: string
  state: ToolState
  input?: unknown
  output?: unknown
  error?: string
}

type ChatMessage = {
  id: string
  role: ChatRole
  content: string
  tools?: ToolTrace[]
}

type StreamEvent =
  | { event: "response.started"; data: Record<string, never> }
  | { event: "response.delta"; data: { text: string } }
  | { event: "response.completed"; data: { text: string } }
  | {
      event: "response.error"
      data: { message: string; code?: string }
    }
  | {
      event: "tool.started"
      data: { id: string; name: string; input?: unknown }
    }
  | {
      event: "tool.completed"
      data: {
        id: string
        name: string
        status: "completed" | "error"
        input?: unknown
        output?: unknown
      }
    }

type SessionProfile = {
  name: string
  username?: string
}

type SessionInfo = {
  authenticated: boolean
  profile?: SessionProfile
  csrfToken?: string
  historyKey?: string
  model: string
  mcp: {
    name: string
    tools: string[]
  }
}

const HISTORY_PREFIX = "order-agent-chat-history"
const THEME_KEY = "order-agent-theme"
const STORED_MESSAGE_LIMIT = 100
const suggestions = [
  "What orders do I have?",
  "Create an order for two DEMO-001 items at 1299 cents each.",
  "Prove which Graph identity the MCP server resolved for me.",
]

const identitySteps = [
  {
    icon: UserRound,
    label: "Signed-in user",
    detail: "Delegated Entra session",
    tone: "identity",
  },
  {
    icon: Bot,
    label: "Agent runtime",
    detail: "Microsoft Foundry",
    tone: "protocol",
  },
  {
    icon: Wrench,
    label: "Order MCP",
    detail: "User token forwarded",
    tone: "protocol",
  },
  {
    icon: Database,
    label: "Microsoft Graph",
    detail: "Token exchange + Graph /me",
    tone: "identity",
  },
] as const

type IdentityStep = (typeof identitySteps)[number]

function storedTheme(): Theme {
  const saved = localStorage.getItem(THEME_KEY)
  if (saved === "light" || saved === "dark") {
    return saved
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light"
}

function useTheme() {
  const [theme, setTheme] = useState<Theme>(storedTheme)

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark")
    document.documentElement.style.colorScheme = theme
    document
      .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
      ?.setAttribute("content", theme === "dark" ? "#030406" : "#eff2f6")
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  return {
    theme,
    toggle: () => setTheme((current) => (current === "dark" ? "light" : "dark")),
  }
}

function historyStorageKey(historyKey: string) {
  return `${HISTORY_PREFIX}:${historyKey}`
}

function clearOtherHistories(activeKey?: string) {
  const activeStorageKey = activeKey ? historyStorageKey(activeKey) : null
  for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = sessionStorage.key(index)
    if (
      key?.startsWith(HISTORY_PREFIX) &&
      key !== activeStorageKey
    ) {
      sessionStorage.removeItem(key)
    }
  }
}

function storedMessages(historyKey: string): ChatMessage[] {
  try {
    const value = sessionStorage.getItem(historyStorageKey(historyKey))
    const parsed = value ? (JSON.parse(value) as unknown) : []
    if (!Array.isArray(parsed)) {
      return []
    }
    return parsed
      .filter(
        (message): message is ChatMessage =>
          typeof message === "object" &&
          message !== null &&
          typeof message.id === "string" &&
          ["user", "assistant", "error"].includes(String(message.role)) &&
          typeof message.content === "string"
      )
      .flatMap((message) => {
        const tools = (message.tools ?? []).map((tool) =>
          tool.state === "running"
            ? {
                ...tool,
                state: "error" as const,
                error: "Interrupted before completion.",
              }
            : tool
        )
        if (
          message.role === "assistant" &&
          !message.content &&
          tools.length === 0
        ) {
          return []
        }
        return [{ ...message, tools }]
      })
  } catch {
    return []
  }
}

function parseStreamEvent(block: string): StreamEvent | null {
  let event = ""
  const data: string[] = []
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim()
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart())
    }
  }
  if (!event || data.length === 0) {
    return null
  }
  return {
    event,
    data: JSON.parse(data.join("\n")) as unknown,
  } as StreamEvent
}

async function readEventStream(
  response: Response,
  onEvent: (event: StreamEvent) => void
) {
  if (!response.body) {
    throw new Error("The streaming response had no body.")
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""

  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n")
      let boundary = buffer.indexOf("\n\n")
      while (boundary >= 0) {
        const event = parseStreamEvent(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
        if (event) {
          onEvent(event)
        }
        boundary = buffer.indexOf("\n\n")
      }
      if (done) {
        break
      }
    }

    const trailing = parseStreamEvent(buffer.trim())
    if (trailing) {
      onEvent(trailing)
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

function readableToolName(name: string) {
  return name
    .split("_")
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ")
}

function parsedToolValue(value: unknown) {
  if (typeof value !== "string") {
    return value
  }
  try {
    return JSON.parse(value) as unknown
  } catch {
    return value
  }
}

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("")
}

function BrandMark({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground",
        className
      )}
      aria-hidden="true"
    >
      <Boxes className="size-4" />
    </span>
  )
}

function ThemeToggle({
  theme,
  onToggle,
}: {
  theme: Theme
  onToggle: () => void
}) {
  const label = theme === "dark" ? "Use light theme" : "Use dark theme"
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={label}
          onClick={onToggle}
        >
          {theme === "dark" ? <Sun /> : <Moon />}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function ConnectionStatus({ label = "Session active" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-xs font-medium text-muted-foreground">
      <span className="relative flex size-2" aria-hidden="true">
        <span className="absolute inline-flex size-full rounded-full bg-success/30" />
        <span className="relative inline-flex size-2 rounded-full bg-success" />
      </span>
      {label}
    </span>
  )
}

/* One step renderer for both the signed-out panel and the signed-in rail, so
   the green=identity / blue=protocol language cannot drift between views. */
function IdentityStepRow({
  step,
  connected = false,
}: {
  step: IdentityStep
  connected?: boolean
}) {
  const Icon = step.icon
  return (
    <li className={cn("relative flex gap-3", connected ? "pb-6" : "items-center")}>
      {connected && (
        <span
          className="absolute top-8 bottom-0 left-4 w-px bg-border"
          aria-hidden="true"
        />
      )}
      <span
        className={cn(
          "relative z-10 flex size-8 shrink-0 items-center justify-center rounded-md border bg-card",
          step.tone === "identity"
            ? "border-success/35 text-success"
            : "border-protocol/35 text-protocol"
        )}
      >
        <Icon className="size-4" />
      </span>
      <span className={cn("min-w-0", connected ? "pt-0.5" : "flex-1")}>
        <span className="block text-sm font-medium">{step.label}</span>
        <span className="block text-xs leading-5 text-muted-foreground">
          {step.detail}
        </span>
      </span>
    </li>
  )
}

function ContextRail({ session }: { session: SessionInfo }) {
  return (
    <aside className="hidden min-h-0 flex-col border-r bg-sidebar lg:flex">
      <div className="flex h-16 items-center gap-3 border-b px-5">
        <BrandMark />
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">Order Agent Lab</p>
          <p className="truncate text-xs text-muted-foreground">Delegated MCP OBO</p>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-6">
        <p className="mb-5 text-xs font-medium text-muted-foreground">
          Identity path
        </p>
        <ol className="relative space-y-1" aria-label="Delegated identity path">
          {identitySteps.map((step, index) => (
            <IdentityStepRow
              key={step.label}
              step={step}
              connected={index < identitySteps.length - 1}
            />
          ))}
        </ol>

        <Separator className="my-2" />
        <div className="space-y-4 pt-5">
          <div>
            <p className="text-xs font-medium text-muted-foreground">Model</p>
            <p className="mt-1 text-sm font-medium">{session.model}</p>
          </div>
          <div>
            <p className="text-xs font-medium text-muted-foreground">MCP tools</p>
            <p className="mt-1 text-sm font-medium">
              {session.mcp.tools.length} available
            </p>
          </div>
        </div>
      </div>

      <div className="border-t px-5 py-4">
        <div className="flex items-center justify-between">
          <ConnectionStatus />
          <ShieldCheck className="size-4 text-success" aria-label="Secure path" />
        </div>
      </div>
    </aside>
  )
}

function TranscriptMessage({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user"
  const isError = message.role === "error"
  const tools = message.tools ?? []

  if (!isUser && !isError && !message.content && tools.length === 0) {
    return <PendingMessage />
  }

  return (
    <Message align={isUser ? "end" : "start"}>
      <MessageAvatar>
        <Avatar className="size-8">
          <AvatarFallback
            className={cn(
              "text-xs",
              isUser
                ? "bg-primary text-primary-foreground"
                : isError
                  ? "bg-destructive/10 text-destructive"
                  : "bg-protocol-soft text-protocol"
            )}
          >
            {isUser ? <UserRound className="size-4" /> : <Bot className="size-4" />}
          </AvatarFallback>
        </Avatar>
      </MessageAvatar>
      <MessageContent>
        <MessageHeader>
          {isUser ? "You" : isError ? "System" : "Order Agent"}
        </MessageHeader>
        {tools.map((tool) => (
          <Tool
            key={tool.id}
            className={cn(
              tool.name === "who_am_i" &&
                tool.state === "completed" &&
                "border-success/40 bg-success-soft transition-colors duration-200"
            )}
          >
            <ToolHeader
              name={readableToolName(tool.name)}
              state={tool.state}
              verified={tool.name === "who_am_i"}
            />
            <ToolContent>
              <ToolInput input={tool.input} />
              <ToolOutput output={tool.output} errorText={tool.error} />
            </ToolContent>
          </Tool>
        ))}
        {message.content && (
          <Bubble
            align={isUser ? "end" : "start"}
            variant={isUser ? "tinted" : isError ? "destructive" : "ghost"}
          >
            <BubbleContent
              className={cn(
                !isUser && !isError && "text-[0.9375rem] leading-7",
                isUser && "max-w-[42rem]"
              )}
            >
              {isUser ? (
                message.content
              ) : isError ? (
                <span role="alert">{message.content}</span>
              ) : (
                <Suspense fallback={<span>{message.content}</span>}>
                  <MarkdownResponse>{message.content}</MarkdownResponse>
                </Suspense>
              )}
            </BubbleContent>
          </Bubble>
        )}
      </MessageContent>
    </Message>
  )
}

function PendingMessage() {
  return (
    <Message>
      <MessageAvatar>
        <Avatar className="size-8">
          <AvatarFallback className="bg-protocol-soft text-protocol">
            <Bot className="size-4" />
          </AvatarFallback>
        </Avatar>
      </MessageAvatar>
      <MessageContent>
        <MessageHeader>Order Agent</MessageHeader>
        <div
          className="space-y-2 px-0 py-1"
          role="status"
          aria-label="Order Agent is working"
        >
          <Skeleton className="h-3 w-52" />
          <Skeleton className="h-3 w-36" />
          <p className="pt-1 text-sm text-muted-foreground">
            Starting the agent and waiting for any required MCP tools.
          </p>
        </div>
      </MessageContent>
    </Message>
  )
}

function EmptyConversation({
  onSuggestion,
  disabled,
}: {
  onSuggestion: (message: string) => void
  disabled: boolean
}) {
  return (
    <div className="flex min-h-full min-w-0 flex-1 items-center overflow-hidden">
      <div className="mx-auto w-full min-w-0 max-w-2xl px-5 py-10 md:px-8">
        <div className="mb-6 flex size-10 items-center justify-center rounded-lg border bg-card text-protocol shadow-[var(--shadow-raised)]">
          <Network className="size-5" />
        </div>
        <h1 className="text-xl font-semibold tracking-tight text-balance">
          Ask about orders. Inspect the identity path.
        </h1>
        <p className="mt-2 max-w-[60ch] text-sm leading-6 text-muted-foreground text-pretty">
          Responses stream from Foundry. When the agent needs order data, each
          tool card shows the MCP call and the Graph identity resolved through OBO.
        </p>
        <div
          className="mt-7 grid gap-2 sm:flex sm:flex-wrap"
          aria-label="Suggested prompts"
        >
          {suggestions.map((suggestion) => (
            <Button
              key={suggestion}
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11 w-full max-w-full justify-start py-2 text-left whitespace-normal sm:w-auto"
              disabled={disabled}
              onClick={() => onSuggestion(suggestion)}
            >
              {suggestion}
            </Button>
          ))}
        </div>
      </div>
    </div>
  )
}

function PromptComposer({
  value,
  pending,
  error,
  onChange,
  onSubmit,
  onCancel,
}: {
  value: string
  pending: boolean
  error: string | null
  onChange: (value: string) => void
  onSubmit: () => void
  onCancel: () => void
}) {
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!pending) {
      inputRef.current?.focus()
    }
  }, [pending])

  return (
    <div className="border-t bg-background px-4 pt-3 pb-[max(1rem,env(safe-area-inset-bottom))] md:px-8">
      <div className="mx-auto max-w-3xl">
        {error && (
          <Alert variant="destructive" className="mb-3">
            <AlertCircle />
            <AlertTitle>Request failed</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault()
            onSubmit()
          }}
        >
          <label
            className="mb-2 block text-sm font-medium"
            htmlFor="order-agent-prompt"
          >
            Ask the Order Agent
          </label>
          <InputGroup className="min-h-24 rounded-xl border-input bg-card shadow-[var(--shadow-raised)] has-disabled:opacity-100">
            <InputGroupTextarea
              ref={inputRef}
              id="order-agent-prompt"
              value={value}
              maxLength={4000}
              rows={2}
              disabled={pending}
              placeholder="Ask about your orders..."
              className="min-h-14 field-sizing-content px-3 pt-3 text-base"
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault()
                  event.currentTarget.form?.requestSubmit()
                }
              }}
            />
            <InputGroupAddon
              align="block-end"
              className="justify-between border-t px-2.5"
            >
              <InputGroupText className="text-xs">
                <KeyRound className="size-3.5" />
                Runs as your identity
              </InputGroupText>
              <InputGroupButton
                type={pending ? "button" : "submit"}
                variant={pending ? "outline" : "default"}
                size="icon-sm"
                aria-label={pending ? "Stop response" : "Send message"}
                disabled={!pending && !value.trim()}
                onClick={pending ? onCancel : undefined}
              >
                {pending ? <Square /> : <ArrowUp />}
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
        </form>
        <p className="mt-2 text-center text-xs text-muted-foreground">
          {pending
            ? "Response is streaming. Stop it at any time."
            : "Enter to send · Shift + Enter for a new line"}
        </p>
      </div>
    </div>
  )
}

function UserMenu({
  profile,
  theme,
  onTheme,
  onLogout,
}: {
  profile: SessionProfile
  theme: Theme
  onTheme: () => void
  onLogout: () => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className="h-11 gap-2 px-2" aria-label="User menu">
          <Avatar className="size-7">
            <AvatarFallback className="bg-primary text-xs text-primary-foreground">
              {initials(profile.name)}
            </AvatarFallback>
          </Avatar>
          <span className="hidden max-w-36 truncate text-sm sm:inline">
            {profile.name}
          </span>
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="space-y-0.5">
          <span className="block truncate text-foreground">{profile.name}</span>
          {profile.username && (
            <span className="block truncate font-normal">{profile.username}</span>
          )}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={onTheme}>
          {theme === "dark" ? <Sun /> : <Moon />}
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </DropdownMenuItem>
        <DropdownMenuItem variant="destructive" onSelect={onLogout}>
          <LogOut />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function SignedOut({
  session,
  theme,
  onTheme,
}: {
  session: SessionInfo
  theme: Theme
  onTheme: () => void
}) {
  return (
    <div className="flex min-h-dvh flex-col bg-background">
      <header className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between border-b px-5 md:px-8">
        <div className="flex items-center gap-3">
          <BrandMark />
          <div>
            <p className="text-sm font-semibold">Order Agent Lab</p>
            <p className="text-xs text-muted-foreground">Delegated MCP OBO</p>
          </div>
        </div>
        <ThemeToggle theme={theme} onToggle={onTheme} />
      </header>

      <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col justify-center gap-12 px-5 py-14 md:px-8 min-[56rem]:grid min-[56rem]:grid-cols-[minmax(0,1fr)_minmax(20rem,25rem)] min-[56rem]:items-center min-[56rem]:gap-10">
        <section>
          <Badge variant="outline" className="mb-5">
            Live delegated identity demo
          </Badge>
          <h1 className="max-w-2xl text-3xl font-semibold tracking-[-0.025em] text-balance md:text-4xl">
            Follow one user identity through an agent and its tools.
          </h1>
          <p className="mt-5 max-w-[65ch] text-base leading-7 text-muted-foreground text-pretty">
            Sign in and ask about sample orders using {session.model}. The chat
            forwards your delegated token to a separate MCP server, which exchanges
            it for a Graph token and resolves your identity before each tool runs.
          </p>
          <Button asChild size="lg" className="mt-8">
            <a href="/login">
              <ShieldCheck />
              Sign in with Microsoft
            </a>
          </Button>
          <p className="mt-4 flex items-center gap-2 text-xs text-muted-foreground">
            <KeyRound className="size-3.5" />
            Access and refresh tokens remain in the server-side session.
          </p>
        </section>

        <section
          className="rounded-xl border bg-workbench p-6 shadow-[var(--shadow-raised)]"
          aria-labelledby="identity-path-heading"
        >
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 id="identity-path-heading" className="text-sm font-semibold">
                Delegated identity path
              </h2>
              <p className="mt-1 text-xs text-muted-foreground">
                Four boundaries, one signed-in identity
              </p>
            </div>
            <ConnectionStatus label="Demo ready" />
          </div>
          <Separator className="my-5" />
          <ol className="space-y-4">
            {identitySteps.map((step) => (
              <IdentityStepRow key={step.label} step={step} />
            ))}
          </ol>
        </section>
      </main>
    </div>
  )
}

function LoadingScreen() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-background px-6">
      <div className="w-full max-w-sm space-y-5" role="status">
        <div className="flex items-center gap-3">
          <BrandMark />
          <div className="space-y-2">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-3 w-24" />
          </div>
        </div>
        <Skeleton className="h-24 w-full rounded-xl" />
        <span className="sr-only">Loading Order Agent</span>
      </div>
    </div>
  )
}

function BootstrapError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-background px-5">
      <Alert variant="destructive" className="max-w-lg">
        <AlertCircle />
        <AlertTitle>Order Agent could not start</AlertTitle>
        <AlertDescription className="mt-2">
          The browser could not read the current application session.
          <Button variant="outline" size="sm" className="mt-4" onClick={onRetry}>
            Try again
          </Button>
        </AlertDescription>
      </Alert>
    </div>
  )
}

function ChatWorkspace({
  session,
  theme,
  onTheme,
}: {
  session: SessionInfo & {
    authenticated: true
    profile: SessionProfile
    csrfToken: string
    historyKey: string
  }
  theme: Theme
  onTheme: () => void
}) {
  const [messages, setMessages] = useState<ChatMessage[]>(() =>
    storedMessages(session.historyKey)
  )
  const [draft, setDraft] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState("")
  const streamAbort = useRef<AbortController | null>(null)

  useEffect(() => {
    const key = historyStorageKey(session.historyKey)
    try {
      sessionStorage.setItem(
        key,
        JSON.stringify(messages.slice(-STORED_MESSAGE_LIMIT))
      )
    } catch {
      sessionStorage.removeItem(key)
    }
  }, [messages, session.historyKey])

  useEffect(
    () => () => {
      streamAbort.current?.abort()
    },
    []
  )

  const cancelStream = useCallback(() => {
    streamAbort.current?.abort("user_stop")
  }, [])

  const sendMessage = useCallback(
    async (rawMessage: string) => {
      const message = rawMessage.trim()
      if (!message || pending) {
        return
      }

      setDraft("")
      setError(null)
      setPending(true)
      setAnnouncement("Order Agent is working")
      const assistantId = crypto.randomUUID()
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "user", content: message },
        { id: assistantId, role: "assistant", content: "", tools: [] },
      ])

      const controller = new AbortController()
      streamAbort.current = controller
      let streamCompleted = false
      const updateAssistant = (
        update: (message: ChatMessage) => ChatMessage
      ) => {
        setMessages((current) =>
          current.map((item) => (item.id === assistantId ? update(item) : item))
        )
      }

      try {
        const response = await fetch("/api/chat/stream", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": session.csrfToken,
          },
          body: JSON.stringify({ message }),
          signal: controller.signal,
        })
        if (response.status === 401) {
          window.location.assign("/login")
          return
        }
        if (!response.ok) {
          const payload = (await response.json().catch(() => ({}))) as {
            error?: string
          }
          throw new Error(payload.error || "The agent request failed.")
        }
        if (
          !response.headers.get("content-type")?.includes("text/event-stream")
        ) {
          throw new Error(
            "The server returned an unreadable response. Try the request again."
          )
        }

        await readEventStream(response, (streamEvent) => {
          if (streamEvent.event === "response.delta") {
            updateAssistant((current) => ({
              ...current,
              content: current.content + streamEvent.data.text,
            }))
          } else if (streamEvent.event === "response.completed") {
            streamCompleted = true
            updateAssistant((current) => ({
              ...current,
              content: current.content || streamEvent.data.text,
            }))
          } else if (streamEvent.event === "tool.started") {
            updateAssistant((current) => ({
              ...current,
              tools: [
                ...(current.tools ?? []).filter(
                  (tool) => tool.id !== streamEvent.data.id
                ),
                {
                  id: streamEvent.data.id,
                  name: streamEvent.data.name,
                  state: "running",
                  input: streamEvent.data.input,
                },
              ],
            }))
            setAnnouncement(
              `Running ${readableToolName(streamEvent.data.name)}`
            )
          } else if (streamEvent.event === "tool.completed") {
            updateAssistant((current) => ({
              ...current,
              tools: (() => {
                const tools = current.tools ?? []
                const existing = tools.find(
                  (tool) => tool.id === streamEvent.data.id
                )
                const completed: ToolTrace = {
                  id: streamEvent.data.id,
                  name: streamEvent.data.name || existing?.name || "tool",
                  state:
                    streamEvent.data.status === "error"
                      ? "error"
                      : "completed",
                  input: streamEvent.data.input ?? existing?.input,
                  output:
                    streamEvent.data.status === "error"
                      ? undefined
                      : parsedToolValue(streamEvent.data.output),
                  error:
                    streamEvent.data.status === "error"
                      ? String(streamEvent.data.output || "Tool call failed.")
                      : undefined,
                }
                return existing
                  ? tools.map((tool) =>
                      tool.id === streamEvent.data.id ? completed : tool
                    )
                  : [...tools, completed]
              })(),
            }))
            setAnnouncement(
              `${readableToolName(streamEvent.data.name)} ${
                streamEvent.data.status === "error" ? "failed" : "completed"
              }`
            )
          } else if (
            streamEvent.event === "response.error" &&
            streamEvent.data.code === "auth_required"
          ) {
            window.location.assign("/login")
            throw new DOMException("Authentication required", "AbortError")
          } else if (streamEvent.event === "response.error") {
            throw new Error(streamEvent.data.message)
          }
        })
        if (!streamCompleted) {
          throw new Error(
            "The response ended before the agent finished. Try the request again."
          )
        }
        setAnnouncement("Order Agent response received")
      } catch (requestError) {
        if (
          requestError instanceof DOMException &&
          requestError.name === "AbortError"
        ) {
          if (controller.signal.reason === "user_stop") {
            setMessages((current) =>
              current.flatMap((item) => {
                if (item.id !== assistantId) {
                  return [item]
                }
                const stopped = {
                  ...item,
                  tools: item.tools?.map((tool) =>
                    tool.state === "running"
                      ? {
                          ...tool,
                          state: "error" as const,
                          error: "Stopped before completion.",
                        }
                      : tool
                  ),
                }
                return [
                  ...(stopped.content || (stopped.tools?.length ?? 0) > 0
                    ? [stopped]
                    : []),
                  {
                    id: crypto.randomUUID(),
                    role: "error" as const,
                    content: "Response stopped. Send another message when ready.",
                  },
                ]
              })
            )
            setAnnouncement("Response stopped")
          }
          return
        }
        const detail =
          requestError instanceof Error
            ? requestError.message
            : "The agent request failed."
        setError(detail)
        setMessages((current) =>
          current.flatMap((item) => {
            if (item.id !== assistantId) {
              return [item]
            }
            if (item.content || (item.tools?.length ?? 0) > 0) {
              return [
                item,
                { id: crypto.randomUUID(), role: "error", content: detail },
              ]
            }
            return [{ ...item, role: "error", content: detail }]
          })
        )
        setAnnouncement("Order Agent request failed")
      } finally {
        if (streamAbort.current === controller) {
          streamAbort.current = null
        }
        setPending(false)
      }
    },
    [pending, session.csrfToken]
  )

  const logout = useCallback(async () => {
    try {
      streamAbort.current?.abort()
      const response = await fetch("/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": session.csrfToken },
      })
      const payload = (await response.json()) as {
        logoutUrl?: string
        error?: string
      }
      if (!response.ok || !payload.logoutUrl) {
        throw new Error(payload.error || "Sign-out failed.")
      }
      sessionStorage.removeItem(historyStorageKey(session.historyKey))
      window.location.assign(payload.logoutUrl)
    } catch (logoutError) {
      const detail =
        logoutError instanceof Error ? logoutError.message : "Sign-out failed."
      setError(detail)
      setAnnouncement("Sign out failed")
    }
  }, [session.csrfToken, session.historyKey])

  return (
    <div className="grid h-dvh min-h-0 w-full overflow-hidden bg-background lg:grid-cols-[17rem_minmax(0,1fr)]">
      <ContextRail session={session} />

      <main className="flex min-h-0 min-w-0 w-full flex-col overflow-hidden">
        <header className="flex h-16 shrink-0 items-center justify-between border-b px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <BrandMark className="lg:hidden" />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">Order conversation</p>
              <div className="mt-0.5 flex items-center gap-2 lg:hidden">
                <span className="truncate text-xs text-muted-foreground">
                  {session.model}
                </span>
                <span className="text-border" aria-hidden="true">
                  /
                </span>
                <ConnectionStatus label="Delegated session" />
              </div>
              <p className="hidden truncate text-xs text-muted-foreground lg:block">
                MCP tools run with {session.profile.name}&apos;s delegated identity
              </p>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <div className="hidden sm:block">
              <ThemeToggle theme={theme} onToggle={onTheme} />
            </div>
            <UserMenu
              profile={session.profile}
              theme={theme}
              onTheme={onTheme}
              onLogout={() => void logout()}
            />
          </div>
        </header>

        <MessageScrollerProvider autoScroll>
          <MessageScroller className="flex-1">
            <MessageScrollerViewport>
              <MessageScrollerContent
                className="mx-auto w-full max-w-3xl px-4 py-6 md:px-8"
                role="log"
                aria-label="Order conversation"
                aria-live="polite"
                aria-relevant="additions text"
              >
                {messages.length === 0 ? (
                  <EmptyConversation
                    disabled={pending}
                    onSuggestion={(message) => void sendMessage(message)}
                  />
                ) : (
                  messages.map((message) => (
                    <MessageScrollerItem
                      key={message.id}
                      messageId={message.id}
                      scrollAnchor={message.role === "user"}
                    >
                      <TranscriptMessage message={message} />
                    </MessageScrollerItem>
                  ))
                )}
              </MessageScrollerContent>
            </MessageScrollerViewport>
            <MessageScrollerButton />
          </MessageScroller>
        </MessageScrollerProvider>

        <PromptComposer
          value={draft}
          pending={pending}
          error={error}
          onChange={setDraft}
          onSubmit={() => void sendMessage(draft)}
          onCancel={cancelStream}
        />
        <div className="sr-only" role="status" aria-live="polite">
          {announcement}
        </div>
      </main>
    </div>
  )
}

function App() {
  const { theme, toggle } = useTheme()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [bootstrapError, setBootstrapError] = useState(false)

  const loadSession = useCallback(async () => {
    setBootstrapError(false)
    try {
      const response = await fetch("/api/session", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
      if (!response.ok) {
        throw new Error("Session bootstrap failed")
      }
      const payload = (await response.json()) as SessionInfo
      clearOtherHistories(
        payload.authenticated && payload.historyKey
          ? payload.historyKey
          : undefined
      )
      setSession(payload)
    } catch {
      setBootstrapError(true)
    }
  }, [])

  useEffect(() => {
    void loadSession()
  }, [loadSession])

  if (bootstrapError) {
    return <BootstrapError onRetry={() => void loadSession()} />
  }
  if (!session) {
    return <LoadingScreen />
  }
  if (
    session.authenticated &&
    session.profile &&
    session.csrfToken &&
    session.historyKey
  ) {
    return (
      <ChatWorkspace
        session={{
          ...session,
          authenticated: true,
          profile: session.profile,
          csrfToken: session.csrfToken,
          historyKey: session.historyKey,
        }}
        theme={theme}
        onTheme={toggle}
      />
    )
  }
  return <SignedOut session={session} theme={theme} onTheme={toggle} />
}

export default function Root() {
  return (
    <TooltipProvider>
      <App />
    </TooltipProvider>
  )
}
