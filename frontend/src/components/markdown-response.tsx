import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"])

// react-markdown's own transform is a string scan that also keeps irc:, ircs:
// and xmpp:, and it is the last thing between model output and the DOM. That
// output is not trusted: a Unity Catalog table COMMENT reaches the model, so a
// URL in the rendered markdown can be attacker-chosen. Parsing rather than
// scanning is what makes `java\nscript:` and other whitespace-split schemes
// resolve to the thing the browser will actually see. Images matter at least as
// much as links — an <img> fetches with no click at all — and react-markdown
// runs this over every URL attribute, src included.
function safeUrl(url: string): string {
  try {
    // Relative URLs have no scheme to check; the base only exists so they parse
    // at all, and the response CSP is what keeps them pointed at this app.
    const parsed = new URL(url.trim(), window.location.origin)
    return SAFE_PROTOCOLS.has(parsed.protocol.toLowerCase()) ? parsed.href : ""
  } catch {
    return ""
  }
}

export default function MarkdownResponse({ children }: { children: string }) {
  return (
    <div className="message-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={safeUrl}
        components={{
          a: ({ children: linkChildren, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {linkChildren}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
