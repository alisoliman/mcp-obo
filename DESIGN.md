---
name: Order Agent
description: A precise identity workbench for demonstrating delegated MCP OBO.
colors:
  background: "oklch(0.961 0.006 250)"
  card: "oklch(1 0 0)"
  surface: "oklch(0.938 0.007 250)"
  surface-strong: "oklch(0.932 0.008 250)"
  ink: "oklch(0.18 0.018 250)"
  muted: "oklch(0.44 0.02 250)"
  border: "oklch(0.78 0.016 250)"
  control-border: "oklch(0.63 0.024 250)"
  primary: "oklch(0.40 0.106 150)"
  primary-hover: "oklch(0.34 0.102 150)"
  protocol: "oklch(0.50 0.15 245)"
  protocol-soft: "oklch(0.93 0.028 245)"
  success: "oklch(0.45 0.12 152)"
  success-soft: "oklch(0.94 0.035 152)"
  danger: "oklch(0.48 0.17 25)"
  dark-background: "oklch(0.108 0.008 255)"
  dark-card: "oklch(0.186 0.010 255)"
  dark-surface: "oklch(0.158 0.009 255)"
  dark-ink: "oklch(0.96 0.005 255)"
  dark-muted: "oklch(0.78 0.015 255)"
  dark-border: "oklch(0.386 0.016 255)"
  dark-control-border: "oklch(0.502 0.022 255)"
  dark-primary: "oklch(0.75 0.17 152)"
  dark-primary-hover: "oklch(0.82 0.16 152)"
  dark-protocol: "oklch(0.74 0.13 245)"
  dark-protocol-soft: "oklch(0.26 0.045 245)"
  dark-success: "oklch(0.78 0.15 152)"
  dark-success-soft: "oklch(0.25 0.055 152)"
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

Cool tinted neutrals keep the technical content crisp. Deep cultivated green
marks primary actions; a dedicated success green marks verified identity state;
steel blue identifies model and protocol context.

Every value below is shipped in `frontend/src/index.css`. That file is the
source of truth; this document mirrors it. `--protocol` is the semantic root —
`--accent` and `--ring` alias it rather than duplicating the value, so a fork
cannot drift them apart.

### Primary

- **Verified Moss** (`oklch(0.40 0.106 150)`): sign-in, send, primary actions.
- **Moss Pressed** (`oklch(0.34 0.102 150)`): primary hover and active state,
  shipped as `--primary-hover`. Hover darkens with a solid token, never opacity.
- Dark theme inverts the relationship: a bright mint (`oklch(0.75 0.17 152)`)
  with near-black label, because a dark-on-dark fill cannot separate from the
  canvas.

### Signal

- **Protocol Blue** (`oklch(0.50 0.15 245)`): Foundry, model, MCP, links, focus.
- **Proof Green** (`oklch(0.45 0.12 152)`): verified identity, completed tool
  calls, live-session dots. Distinct from primary so "do this" and "this is
  proven" never share a treatment.

### Neutral

- **Cool Canvas** (`oklch(0.961 0.006 250)`): the page beneath everything.
- **Pure Card** (`oklch(1 0 0)`): raised reading and tool surfaces.
- **Cool Instrument** (`oklch(0.938 0.007 250)`): rail and workbench panel.
- **Graphite Ink** (`oklch(0.18 0.018 250)`): primary text.
- **Slate Note** (`oklch(0.44 0.02 250)`): supporting text at AA contrast.
- **Fine Rule** (`oklch(0.78 0.016 250)`): separators and container borders.
- **Control Rule** (`oklch(0.63 0.024 250)`): borders of interactive controls,
  held at 3:1 for WCAG 1.4.11.

**The Ten Percent Rule.** Green and blue together occupy less than ten percent
of a screen. Their rarity communicates state instead of decoration.

## Elevation

Depth comes from a measured tonal ramp, reinforced by a one-pixel border and a
small ambient shadow. Canvas sits lowest, the rail and workbench panel sit just
above it, and cards sit highest.

The ramp is not decorative — each adjacent pair is held above a contrast floor
so structure stays legible in bright ambient light and on projectors:

| Pair | Light | Dark | Floor |
|---|---|---|---|
| card vs canvas | 1.12:1 | 1.10:1 | 1.10 |
| rail vs canvas | 1.07:1 | 1.05:1 | 1.05 |
| border vs card | 2.00:1 | 1.91:1 | 1.90 |
| control border vs card | 3.49:1 | 3.13:1 | 3.00 |
| body text vs card | 18.79:1 | 16.56:1 | 4.50 |
| muted text vs card | 7.75:1 | 9.30:1 | 4.50 |

**The Measured Depth Rule.** A surface may only be called elevated if it is
measurably separable from the surface beneath it. Borders alone are not depth.

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

## Elevation Details

The sticky prompt and cards use `--shadow-raised`; menus and the scroll-to-end
control use `--shadow-overlay`. Both are tokens, never inline values.

**The State-Only Shadow Rule.** Shadow reinforces the tonal ramp; it never
substitutes for it, and resting content never floats for decoration.

## Components

### Buttons

- **Shape:** compact medium radius (`0.625rem`), never pill-shaped.
- **Primary:** Verified Moss with white text and 0.625rem by 1rem padding.
- **Hover / Focus:** darken to Moss Pressed; focus uses a two-pixel Protocol Blue
  ring with visible offset.
- **Ghost:** neutral text on transparent background; hover uses Cool Instrument.

### Chips

- **Style:** one-pixel border on the card surface, Slate Note text, no filled
  pill. Status chips pair a state icon with plain text.
- **State:** suggestion chips are keyboard buttons, not decorative tags.
  Connected status uses a small Proof Green dot and text.

### Cards / Containers

- **Corner Style:** large radius (`0.875rem`) for the prompt and signed-out
  workbench panel; medium radius for tool cards.
- **Background:** Pure Card for raised content, Cool Instrument for the rail.
- **Shadow Strategy:** `--shadow-raised`, supporting the tonal ramp.
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
surface. Streamed MCP calls use collapsible shadcn tool cards with explicit
Running, Completed, and Error states. Tool cards are **collapsed by default** and
expand on click with a 200ms height transition; the identity-proof card is
marked with a Proof Green border and tint rather than being force-opened, so the
transcript stays scannable. The pending state is a labeled skeleton row. New
content is announced through a polite live region and the scroller follows only
when the user is at the bottom.

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
- Number an already-ordered list. Sequence markers like `01 / 02 / 03` decorate
  rather than inform when the list order already carries the sequence.
- Rely on a single one-pixel border to carry elevation.
- Express hover as an opacity shift on a filled control; use a solid token.
- Fill the page with architecture cards or production-health metrics.
- Hide failures behind a generic assistant message.
- Put tokens, raw claims, or sensitive identity details in the browser.
- Auto-scroll a reader away from content they intentionally scrolled up to.
