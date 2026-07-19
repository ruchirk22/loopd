# loopd — brand & design system

One logo, one font pair, one accent, one spacing rhythm. Everything loopd ships — the CLI,
the dashboard, the README, the site, screenshots, videos — should look like it came from the
same place. When in doubt, prefer restraint. **Monochrome, calm, effortless.** No gradients,
no neon.

## Logo

- **Mark:** the loopd infinity wordmark. Source files live in `orchestrator/assets/`
  (`loopd.svg`, `loopd_no_bg.png` transparent, `loopd.png`).
- **On dark UI:** render it monochrome (the dashboard applies `grayscale(1)` so the mark is a
  soft white). Don't reintroduce color into the mark inside product surfaces.
- **Favicon:** `loopd.svg`.
- **Clear space:** keep at least the height of the "o" around the mark. Don't crowd it.

## Color

Grayscale is the system. There is exactly **one accent (white)** for emphasis, and **two
whisper hues used only for status** — never as fills or decoration.

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0b0b0c` | app background |
| `--panel` / `--panel-2` / `--raise` | `#141416` / `#101012` / `#17171a` | surfaces |
| `--line` / `--line-2` | `rgba(255,255,255,.07)` / `.12` | hairline borders |
| `--fg` / `--fg-strong` | `#eaeaea` / `#fbfbfb` | text / emphasis (the accent) |
| `--mut` / `--faint` | `#9b9ba0` / `#66666b` | secondary / tertiary text |
| `--attention` | `#d6b16a` | *status only* — "needs you" / paused (muted amber) |
| `--good` | `#8fb28c` | *status only* — delivered / verified (muted sage) |

Light/dark: the product is dark-first. Status is conveyed by a small dot + a label, not by
flooding the UI with color.

## Type

- **UI / prose:** Inter (400/500/600).
- **Code / metrics / mono:** JetBrains Mono (400/500).
- Tight tracking (`letter-spacing: -.006em` body), generous line-height, low contrast.

## Spacing & shape

- Radii: `--r 14px` (cards), `--r2 10px` (inputs), `--r3 7px` (chips/steps).
- Rhythm: 14–22px between elements; cards breathe (18–24px padding). Whitespace is the point.

## Motion

Subtle, ≤ ~280ms, ease-out (`cubic-bezier(.2,.6,.2,1)`), no bounce. The only ambient motion
is the status dot's gentle breathing pulse when working.

## Voice

- **CLI = talking *to* your engineer.** First person: "I've got it from here."
- **Dashboard = watching *over its shoulder*.** Third person: "Your engineer finished the work."
- Warm, terse, senior. Never a chatbot; never an error dump. Every line should raise confidence.

These tokens are the source of truth for the dashboard (`orchestrator/dashboard.py` `:root`).
Change them here and there together.
