---
name: Order Agent
description: A precise identity workbench for demonstrating delegated MCP OBO.
colors:
  background: "oklch(1 0 0)"
  surface: "oklch(0.972 0.004 250)"
  surface-strong: "oklch(0.94 0.008 250)"
  ink: "oklch(0.18 0.018 250)"
  muted: "oklch(0.45 0.02 250)"
  border: "oklch(0.88 0.01 250)"
  primary: "oklch(0.40 0.106 150)"
  primary-hover: "oklch(0.34 0.102 150)"
  accent: "oklch(0.54 0.14 245)"
  success: "oklch(0.47 0.11 150)"
  danger: "oklch(0.48 0.17 25)"
  dark-background: "oklch(0.11 0 0)"
  dark-surface: "oklch(0.16 0.008 250)"
  dark-ink: "oklch(0.95 0.006 250)"
  dark-primary: "oklch(0.43 0.11 150)"
  dark-muted: "oklch(0.80 0.014 250)"
typography:
  headline:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 550
    lineHeight: 1.4
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "0.8125rem"
    fontWeight: 450
    lineHeight: 1.5
rounded:
  sm: "0.375rem"
  md: "0.625rem"
  lg: "0.875rem"
spacing:
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "1.5rem"
  xl: "2rem"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.background}"
    rounded: "{rounded.md}"
    padding: "0.625rem 1rem"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.background}"
    rounded: "{rounded.md}"
    padding: "0.625rem 1rem"
  prompt:
    backgroundColor: "{colors.background}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "0.75rem"
  status:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    padding: "0.25rem 0.5rem"
---

# Design System: Order Agent

## Overview

**Creative North Star: "The Identity Workbench"**

The interface should feel like a well-made engineering tool under clear office
light: quiet surfaces, exact labels, strong hierarchy, and no decorative AI
effects. Conversation remains the task, while a restrained context rail makes
the delegated identity path understandable at a glance.

The system rejects generic ChatGPT composition, neon gradients, glass panels,
and dashboard density. It uses familiar controls and exposes model, MCP, and
identity state only when they help someone understand the demonstration.

**Key characteristics:**

- Light-first restrained palette with complete dark-mode tokens.
- Compact product typography with generous reading line-height.
- A structural context rail rather than stacked status cards.
- Message and prompt primitives copied through shadcn and adapted locally.
- State motion only, always disabled under reduced-motion preferences.

## Colors

Pure neutral surfaces keep the technical content crisp. Deep cultivated green
marks primary actions and verified identity state; steel blue identifies model
and protocol context.

### Primary

- **Verified Moss** (`oklch(0.40 0.106 150)`): sign-in, send, active identity,
  and successful secure-path emphasis.
- **Moss Pressed** (`oklch(0.34 0.102 150)`): primary hover and active state.

### Secondary

- **Protocol Blue** (`oklch(0.54 0.14 245)`): Foundry, model, MCP, links, and
  focus-visible accents.

### Neutral

- **Pure Canvas** (`oklch(1 0 0)`): page and message reading surface.
- **Cool Instrument** (`oklch(0.972 0.004 250)`): rail, controls, and quiet
  system context.
- **Graphite Ink** (`oklch(0.18 0.018 250)`): primary text.
- **Slate Note** (`oklch(0.45 0.02 250)`): supporting text at AA contrast.
- **Fine Rule** (`oklch(0.88 0.01 250)`): separators and control borders.

**The Ten Percent Rule.** Green and blue together occupy less than ten percent
of a screen. Their rarity communicates state instead of decoration.

## Typography

**Display Font:** Inter, with the system UI stack as fallback  
**Body Font:** Inter, with the system UI stack as fallback  
**Label/Mono Font:** The platform monospace stack for inline order identifiers

**Character:** One disciplined humanist sans keeps the interface credible and
consistent. Weight and spacing create hierarchy; oversized display type does
not belong in this product.

### Hierarchy

- **Headline** (650, 1.25rem, 1.3): product name and empty-state heading.
- **Title** (600, 1rem, 1.4): context groups and message labels.
- **Body** (400, 0.9375rem, 1.6): conversation and explanatory copy, capped at
  72ch.
- **Label** (550, 0.8125rem, 1.4): status, metadata, and controls.

**The Plain Language Rule.** Labels name the actual system part or action.
Avoid vague AI words such as "magic", "intelligence", and "thinking".

## Elevation

The design is flat by default. Depth comes from tonal layers and one-pixel
borders. The sticky prompt uses a small ambient shadow only to distinguish it
from scrolling content; menus may use the standard shadcn overlay shadow.

**The State-Only Shadow Rule.** Resting content never floats for decoration.

## Components

### Buttons

- **Shape:** compact medium radius (`0.625rem`), never pill-shaped.
- **Primary:** Verified Moss with white text and 0.625rem by 1rem padding.
- **Hover / Focus:** darken to Moss Pressed; focus uses a two-pixel Protocol Blue
  ring with visible offset.
- **Ghost:** neutral text on transparent background; hover uses Cool Instrument.

### Chips

- **Style:** one-pixel border, Cool Instrument background, Slate Note text.
- **State:** suggestion chips are keyboard buttons, not decorative tags.
  Connected status uses a small green dot and text rather than a filled pill.

### Cards / Containers

- **Corner Style:** large radius (`0.875rem`) only for the prompt and signed-out
  explanation panel.
- **Background:** Pure Canvas or Cool Instrument.
- **Shadow Strategy:** flat at rest.
- **Border:** Fine Rule, one pixel.
- **Internal Padding:** 1rem to 1.5rem.

### Inputs / Fields

- **Style:** white textarea in a bordered prompt group with a large radius.
- **Focus:** Protocol Blue border and visible outer ring.
- **Error / Disabled:** danger text is explicit; disabled state preserves
  readable contrast and names the current action.

### Navigation

The desktop context rail is structural, not a dashboard. It presents the four
identity hops in order and collapses into compact header metadata below the
desktop breakpoint.

### Conversation

Use shadcn `MessageScroller`, `Message`, and `Bubble` primitives. User messages
align right with a quiet primary tint. Agent messages align left on the reading
surface. The pending state is a labeled skeleton row. New content is announced
through a polite live region and the scroller follows only when the user is at
the bottom.

## Do's and Don'ts

### Do

- Show who is signed in and state that MCP calls run on that user's behalf.
- Keep the composer visible while the conversation scrolls independently.
- Teach the empty state with three concrete order prompts.
- Render agent Markdown with safe links and readable lists.
- Preserve keyboard focus and restore it to the prompt after sending.
- Respect system dark mode and reduced-motion settings.

### Don't

- Recreate ChatGPT's centered blank-screen composition.
- Use gradients, glow, glass, sparkles, or animated thinking theatrics.
- Fill the page with architecture cards or production-health metrics.
- Hide failures behind a generic assistant message.
- Put tokens, raw claims, or sensitive identity details in the browser.
- Auto-scroll a reader away from content they intentionally scrolled up to.
