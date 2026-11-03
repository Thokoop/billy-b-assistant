# Web UI design system

This document describes the design system of the web configuration UI
(`application/webconfig/`) and the rules for extending it.

- **Canonical** values are the default for every element. They were taken from
  the most common style in the code, and the code has been aligned to them.
- **Exceptions** are the few places that deliberately differ. Each one is listed
  with its reason. An exception is not a variant you can pick for new work.

A rendered preview of every component is in `docs/design-system.html`. Open it
straight from the repository in a browser; it loads the compiled `styles.css`,
so rebuild the CSS first when `input.css` changed. Update the preview together
with this document.

Sources of truth:

| What | Where |
| --- | --- |
| Tokens, component classes, global overrides | `tailwind/src/input.css` |
| Page shell, footer navigation | `templates/base.html` |
| Header, toolbar, power menu | `templates/components/header.html` |
| Status badge macro | `templates/components/status-badge.html` |
| Notification bar, tooltips, dropdowns | `static/js/ui-helpers.js` |
| Compiled CSS (generated, do not edit) | `static/css/styles.css`, built with `npm run build:css` |

The UI is dark only (`<html class="dark">`), built with Tailwind 4 utilities
plus a set of hand-written component classes. Use a component class whenever
one exists for the element. Use utilities for layout (flex, grid, gap, margins)
and one-off spacing only.

`templates/components/settings-panel.html`, `profile-panel.html` and
`profile-overview.html` are no longer included by any page or route (the last
includes were removed in the settings/profile rework). They were left as they
are and are not part of the system. `log-panel.js` still looks up
`#settings-panel`, which no longer exists; the lookup is null-safe.

---

## 1. Foundations

### 1.1 Colour

The palette is Tailwind's **zinc** greys on top of an animated ocean gradient,
with four accent hues. Each accent has one meaning. These roles are fixed;
`AGENTS.md` also records them.

| Role | Hue | Typical shades | Used for |
| --- | --- | --- | --- |
| Positive / primary | emerald | 600 fill, 500 slider fill, 400 text, 300 light text | Primary actions, selection, active nav, "on" toggles, connected state, sliders |
| Info / focus / live | cyan | 500 ring, 400 hover text, 300 focus outline | Focus, link-like hovers, subtitles, info notifications |
| Warning / maintenance | amber | 600 fill, 400 text, 300 light text | Debug, restart, repair, update, unsaved changes, `.env` editing |
| Destructive / error | rose | 600 fill, 400 text, 300 light text | Delete, reset, errors, support card |

Secondary hues exist only inside their own component: violet
(`status-badge--beta`, waveform lane), orange (`status-badge--deprecated`),
yellow/green (waveform lanes).

**Tokens** (`@theme static` in `input.css`; also available as utilities such as `text-ink-muted`):

| Token | Value | Used for |
| --- | --- | --- |
| `--color-ink` | zinc-200 | Light text inside component CSS |
| `--color-ink-muted` | zinc-400 | Muted text inside component CSS; the same grey as `text-zinc-400` |
| `--color-focus` | cyan-300 | Focus outline |

**Surfaces** (darkest to lightest):

| Name | Value | Used for |
| --- | --- | --- |
| overlay | `bg-black/70` + `backdrop-blur-sm` | Modal backdrop |
| card | `bg-zinc-900/50` + `backdrop-blur-xs` | Cards, sections, modal panels (glass) |
| inset | `bg-zinc-900/40`, no blur | Settings menu; older inset panels (see 5) |
| field | `bg-zinc-800` | Inputs, selects, list rows, selection cards |
| read-only | `bg-zinc-800/50` | Read-only values shown in a field's place |
| raised | `bg-zinc-700` | Slider tracks, chips |
| hover | `white/8` | Hover on secondary buttons and menu items |
| menu | `#142e3b` + `white/14` border | Menu panels |

**Borders:** `border border-zinc-700` is the default border. `white/12` is the
border of secondary buttons; `white/7` the hairline inside lists.

**Text:** neutral zinc greys only; no blue-grey (slate) tints.

| Name | Value | Used for |
| --- | --- | --- |
| primary | `text-white` | Headings, values, button labels |
| label | `text-zinc-300` | Form labels and small sub-headings |
| value | `text-zinc-200` | Readouts next to sliders, section headings, toolbar labels |
| muted | `text-zinc-400` (`--color-ink-muted` in component CSS) | Hints, secondary text |
| subtle | `text-zinc-500` | Meta, disabled-looking text |

Exceptions:

- Mood colours (`mood-panel.js`) and the persona trait level bars
  (`persona-form.js`) are data scales and keep their own red/yellow steps.

### 1.2 Typography

- Font: Tailwind `font-sans` (system stack), set on `<body>`. Monospace
  (`font-mono`) only for code, logs, `.env` editing, MAC/IP addresses and the
  Bluetooth pairing code.
- Weights: regular for body; 500 for buttons; 600 for panel titles, badges,
  group labels and names. Weights live in component classes.

| Step | Size | Used for |
| --- | --- | --- |
| `text-xs` | 12px | Hints, badges, meta, nav labels |
| `text-sm` | 14px | **Default UI text**: labels, inputs, buttons, body copy |
| `text-base` | 16px | Captions text, header name |
| `text-lg` | 18px | Card, section and modal headings; back-button icons |
| `text-xl` | 20px | Settings panel and menu titles (1.25rem in CSS); icons in icon buttons |
| `text-3xl` / `text-4xl` | 30 / 36px | Footer navigation icons (mobile / desktop) |
| Group label | 12px, 600, uppercase, `letter-spacing: .06em` | `.settings-menu-group > h3`, Bluetooth request heading |

Exception: the counter bubble on wake-up clip moods uses `text-[10px]`; it has
to fit a 16px circle.

### 1.3 Spacing

Tailwind's 4px scale. Component CSS stays on multiples of `.125rem`.

- Gap: `gap-2` (8px) is the default; `gap-3` (12px) between groups;
  `gap-4` (16px) between form columns.
- Card padding: `p-4` (16px). Settings cards get `1.5rem` from 1100px up.
- Field padding: `p-3` (12px).
- Card spacing: `mb-4` between stacked cards.
- Label → field: `mt-1` on the field.

Exception: the song waveform editor uses px spacing throughout (dense editor
with its own grid).

### 1.4 Radius

| Step | Value | Used for |
| --- | --- | --- |
| `rounded` | 4px | Fields, chips |
| `rounded-lg` | 8px | Cards, sections, modals, selection cards, inset panels |
| action | 10px (`.625rem`) | Buttons, split actions, settings menu items |
| large | 12px (`.75rem`) | Header toolbar buttons, menus, save bar, info panels, live captions, Bluetooth pairing request |
| `rounded-full` | pill | Badges, toggles, slider bars, status dots, back pills |
| dock | 24px / 16px (`rounded-3xl` / `rounded-2xl`) | Floating footer dock and its items only |

No other radii are used. The dock steps are reserved for the floating footer
navigation, which needs softer corners than the cards but is not a pill; the
item radius plus the dock padding gives the dock radius, so the corners are
concentric. The waveform editor uses `rounded` (4px).

### 1.5 Elevation and glass

- Cards: `shadow-lg`. Modals and sticky panels: `shadow-2xl`.
- Glass: `backdrop-blur-xs` on cards and panels, `backdrop-blur-sm` on the
  modal backdrop. Filled buttons use `blur(10px) saturate(145%)`.
- Filled buttons and active secondary buttons have an inner top highlight plus a
  tinted outer glow; see 3.2.
- The reduce-motion preference (`.reduce-motion`) turns the blur off and swaps
  in a solid `bg-zinc-900`.

### 1.6 Motion

- Colour, border, background, shadow and press transitions: `150ms ease`.
- Reveal/expand: `240ms cubic-bezier(0.22, 1, 0.36, 1)`; disclosure chevrons
  and badge info `180ms`.
- Toggle thumb: `0.2s cubic-bezier(0.34, 1.56, 0.64, 1)` (a small overshoot).
- Press: `transform: translateY(1px)` on `:active`.
- Background: the 60s `gradient-ocean` animation on `<body>`.
- Every animation stops under both `prefers-reduced-motion` and the in-app
  `.reduce-motion` class.

### 1.7 Icons

Material Icons (self-hosted ligature font, `.material-icons`). Default size
24px; 18px inside buttons; 20px in the header, settings menu, icon buttons and
for help icons (`help_outline`) in labels; 16px inside badges and component
state rows. Help icons are 20px so they fit the 20px label line: a label with
or without one has the same height, and the fields below line up. Icons that are pure decoration
get `aria-hidden="true"`; icon-only buttons need an accessible name.

### 1.8 Focus

- Components: `outline: 2px solid var(--color-focus); outline-offset: 3px` on
  `:focus-visible`.
- Fields: `focus:outline-none focus:ring-2 focus:ring-cyan-500`.

Exception: the recovery reset disclosure uses a rose outline, because it opens
a destructive action.

### 1.9 Layers (z-index)

| Layer | z-index | What |
| --- | --- | --- |
| Card with an open tooltip or help panel | 15 | Lifts above the cards after it |
| Sticky save bar | 20 | Stays above page content, including lifted cards |
| Menus, dropdowns | 20–40 | `.dropdown-menu` 20, power menu 40 |
| Footer navigation dock | 30 | Always above content and the save bar's shadow |
| Modals, full-screen log | 50–60 | Above everything |

Content never goes above 15, so the save bar and the dock stay on top.

---

## 2. Layout

### 2.1 Page shell (`base.html`)

- `<body class="text-white font-sans">` over the ocean gradient.
- Content width: `max-width: 72rem` (header, log panel, captions), centred.
- Sticky header (`.billy-header`, see 3.13) at the top; floating dock
  navigation (`<footer>`) fixed at the bottom. Pages keep `pb-32` so content
  clears it.
- Breakpoints: use 480 (small phone), 768 (`md`, split layouts) and 1100 (wide
  settings) for new work. Older component CSS also uses 360, 420, 600 and 640.

### 2.2 Settings workspace

`.settings-workspace` is a one-column grid that becomes `240px | 1fr` at
768px and `280px | 1fr` at 1100px.

- `.settings-menu`: inset surface, sticky below the header, with
  `.settings-menu-group > h3` group labels and `.settings-menu-item` rows
  (44px). The current item has `aria-current="true"`: emerald text,
  emerald/50 border, emerald/12 background.
- Status dots in the menu: `.settings-dirty-dot` (amber, unsaved) and
  `.settings-update-dot` (amber with a number).
- `.settings-panel-title`: icon (emerald) + title; an optional badge is pushed
  to the right.
- `.settings-save-bar`: sticky bar at the bottom, above the footer dock. It is
  zinc-900 at 88% with a 12px blur, so text scrolling underneath cannot be read
  through it.
  The status text turns amber with `.has-changes`.
- Mobile: the menu and the panel are split views with a `.settings-back` button.

---

## 3. Components

### 3.1 Card / section

```html
<section class="card">
  <h4 class="card-title mb-1">Outgoing calls</h4>
  <p class="text-xs text-zinc-400 mb-4">One line on what this group does.</p>
  …
</section>
```

`.card` is the glass card: zinc-900/50 with blur, zinc-700 border, 8px radius,
16px padding (24px in the settings workspace from 1100px), `shadow-lg`. The
last child's bottom margin is dropped, so a trailing `mb-4` row does not add to
the padding. Older cards still spell it out with utilities
(`bg-zinc-900/50 backdrop-blur-xs border border-zinc-700 rounded-lg p-4 shadow-lg`);
both look the same.

**Cards are never nested.** There is one card level:

- A settings panel with several groups uses `settings-card settings-card--sections`.
  The panel then has no card of its own: every group is a separate `.card`,
  stacked with `space-y-4`, and the panel title (`.settings-panel-title`) sits
  inside the first card, never on its own above the cards. A device panel
  template renders `{{ panel_heading }}` at the top of its first section; it
  holds the title for the first device panel and is empty otherwise. The Device panel
  (device-profile sections plus a "General" card), the Software panel and the
  Bluetooth panel (devices, plus a Ringtones card on the fish) work this way.
- A small subgroup inside a card (the GPT-Live model choice, the wake-word file
  upload) is separated by a hairline: `.card-divider` (a zinc-700 top border
  with 16px above and below), or `.api-model-panel` for the bottom border
  variant. It gets no background of its own.
- **Collapsible section**: add `collapsible-section`. The heading is an `h3`
  that stays visible; any other child is hidden while `.collapsed`, except
  elements marked `data-disclosure-panel`. Heading:
  `flex items-center justify-between gap-2 mb-4 text-lg text-zinc-200 hover:text-emerald-400 cursor-pointer transition-colors`.
- **List row** (memories, moods, ringtones): `p-3 bg-zinc-800/50 rounded-lg border border-zinc-700`.
  Rows are list items, not cards, so they may sit inside a card. Row actions
  are icon buttons (`button-icon`, see 3.2); when a row lacks an action its
  neighbours have, a `w-11` spacer keeps the columns aligned.

Exceptions:

- Log toolbar and log panel: `bg-zinc-900/88` with `backdrop-blur-sm`. They
  overlay page content, so they need to be nearly opaque.
- Maintenance overlay card in `base.html`: `bg-zinc-900/80`, shown on a black
  backdrop while the service restarts.
- Empty states: a dashed `border-zinc-700` box on `bg-zinc-900/30`.
- Example songs: `border-amber-600/50`, to mark them as built-in examples.
- **Clickable card** (Recovery & reset): a normal `.card` at rest; on hover the
  border turns rose-400 and the card gets a rose/20 tint, because it leads to
  advanced, destructive options.
- `.library-section-heading` is a card header used only on the library page.
- Still nested (see section 5): the library page columns, the song editor's
  audio file and tail panels, the recovery modal sections and the wake-up clip
  rows.

### 3.2 Buttons

All buttons share one size rule (`.primary-action`, `.secondary-action`):
min-height **44px**, padding `.625rem .75rem`, radius 10px, text 14px / weight
500, icon 18px, gap 8px. Disabled: `opacity: .45`, `cursor: not-allowed`.
Elements with `hidden` stay hidden.

| Kind | Markup | When |
| --- | --- | --- |
| Primary | `primary-action bg-emerald-600 text-white` | The main action of a view (Save, Add, Connect) |
| Warning | `primary-action bg-amber-600 text-white` | Restart, repair, update |
| Destructive | `primary-action bg-rose-600 text-white` | Delete, reset (confirm first) |
| Secondary | `secondary-action secondary-action--hover--cyan` | Everything else |
| Secondary, positive | `secondary-action secondary-action--hover--emerald` | Add, play, test, "Use" in a list |
| Secondary, warning | `secondary-action secondary-action--hover--amber` | Edit, debug, logs, restart |
| Secondary, destructive | `secondary-action secondary-action--hover--rose` | Remove, disconnect, delete |

Label colour on filled buttons: always white.

The filled look (gradient, edge, glow) comes from one rule for
`button|a|label` with `bg-{emerald,cyan,amber,rose}-600`. Each hue sets a few
RGB variables (`--fill`, `--fill-top`, `--fill-hover`, `--fill-edge`,
`--fill-edge-hover`); the drawing is shared.

Secondary tones work the same way. A tone class sets `--tone` (text/border)
and `--tone-deep` (tint/glow):

- `secondary-action--hover--{cyan,emerald,amber,rose}`: colour on hover.
- `secondary-action--{cyan,emerald,amber,rose}`: tinted at rest, for a running
  state (service Start, microphone recording).
- `secondary-action--active`, `--active-cyan`, `--active-amber`: pressed or
  toggled on (emerald, subtitles, debug).

Modifiers:

- `.button-small`: 36px high, padding `.375rem .625rem`, same text size.
- `.button-icon`: square, no padding. Icon-only buttons need `aria-label` or
  `title`.
- `.button-reveal` + `.button-reveal-label`: icon button whose label slides
  out after a 1s hover (immediately on keyboard focus), from 768px up.
  `.button-reveal--wide` allows an 8rem label instead of 5rem.
- Header toolbar buttons are fixed at 44×44px with 12px radius.

**Split action** (a primary action with a menu):

```html
<div class="relative inline-block">
  <div class="split-action">
    <button class="primary-action bg-emerald-600 text-white">…Save</button>
    <button class="primary-action bg-emerald-600 text-white button-icon dropdown-toggle"
            onclick="toggleDropdown(this)" aria-label="More save options">…expand_more</button>
  </div>
  <div class="dropdown-menu menu-panel absolute bottom-full right-0 mb-1 w-full z-20 hidden">…</div>
</div>
```

The menu opens above the button, so the chevron stays visible and closes it
again. Clicking outside also closes it.

Exception: the waveform editor's play (34px) and zoom (22px) buttons are below
the 44px touch target; the editor is a dense desktop tool.

### 3.3 Form fields

**Text input, select, textarea:** `.field`

```html
<input class="field mt-1">
```

`.field` is full width, 12px padding, zinc-800, 4px radius, with a cyan focus
ring. Inputs and selects are exactly 48px high, so a select and an input side by
side line up. It lives in `@layer components`, so utilities on the element still
win (`mt-1`, `pr-10`, `font-mono`, `text-amber-500`). A password field adds
`pr-10` and an `.input-reveal-button` with a `visibility` icon. The same field
sits on cards and in modals.

- **Compact field** for toolbars (log level, log source): `field field--compact`
  (32px, 14px text, auto width).
- **Read-only value** in a field's place: `.field-static` (48px, zinc-800/50,
  muted text, ellipsis). Add `font-mono text-white` for values to copy, such as
  the device time.

**Label:**

```html
<label for="…" class="flex justify-between items-center text-sm text-zinc-300 relative">
  Label <span class="material-icons …" onclick="toggleTooltip(this)">help_outline</span>
</label>
```

Use `block text-sm text-zinc-300` when there is no help icon. `relative` is
needed when a tooltip is anchored to the label.

Spacing between label and field is always the field's `mt-1`; labels carry no
bottom margin. Help icons are 20px with a 20px line height, so labels with and
without one are the same height. A toggle or slider that sits under a label in
a field row is centred on the 48px field line.

**Hint:** `text-xs text-zinc-400`, with `mt-1` below a field. Inside component
CSS, hints use `.75rem var(--color-ink-muted)` (`.api-field-hint`).

**Field grid:** `.api-settings-grid` / `.api-model-grid`, auto-fit columns of at
least 13rem, gap 1rem, cells aligned to the top. A toggle setting
(`.api-toggle-setting`: label, help icon, toggle on one 24px line) placed in
the grid sits on the label line of its neighbours.

Exception: the `.env` editor textarea has amber text and an amber border, to
mark it as a maintenance tool.

### 3.4 Help: tooltip and inline help

- **Tooltip** (`[data-tooltip]`): click a `help_outline` icon
  (`hover:text-cyan-400`) to show a small panel, 12px dark text on
  `bg-cyan-400/70` with blur, max 350px. All trigger icons can be hidden with
  the "Show tooltip icons" setting (`.tooltips-hidden`). Use it for short,
  plain explanations.
- **Inline help** (`details.settings-inline-help`): a 2rem round `summary`
  button opening `.settings-help-panel`. Variant `.settings-warning-help` is
  amber. Use it when the text needs links or emphasis, or is a warning.

### 3.5 Toggle switch (`.wf-toggle`)

```html
<label class="wf-toggle" title="…">
  <input type="checkbox" aria-label="…">
  <span class="wf-toggle-track"><span class="wf-toggle-thumb"></span></span>
</label>
```

42×24px, zinc-600 track, emerald when checked, cyan focus outline. When a
setting is stored as a `<select>`, the select stays hidden and the toggle
drives it (`.api-toggle-setting`). Bluetooth device rows use a compact
34×20px version. The `wf-` prefix comes from the waveform editor, where the
toggle started.

### 3.6 Slider and stepped control

- **Value slider**: a hidden `<input type="range">` plus a visible bar,
  `relative w-full h-4 rounded-full bg-zinc-700` with an emerald fill
  (`bg-emerald-500`). Used for gain, volume, LED brightness and mouth boost.
- **Stepped control** (`.settings-step-control`): the same bar for a hidden
  select, with white dots on the steps and a label below.
- Native range inputs that remain (persona form, waveform zoom) use an emerald
  thumb on a zinc-700 track, matching the bar.

### 3.7 Selection card (`.selection-card`)

The shared selectable row for songs, personas, profiles and Bluetooth devices:
`zinc-800` background, `zinc-700` border, radius 8px. Hover and
`data-selected="true"` turn the border emerald; selected adds an emerald/10
background. A `.selection-card-trigger` button stretches over the whole card
for clicks and focus; `.selection-card-actions` sit above it.

### 3.8 Status badge and info panel

Use the Jinja macros in `templates/components/status-badge.html`:

```jinja
{% from "components/status-badge.html" import status_badge, badge_info %}
{{ status_badge('gpt-live', 'Beta', kind='beta') }}
{% call badge_info('gpt-live', kind='beta') %}…{% endcall %}
```

A pill button (12px, weight 600) tinted by `--badge-rgb`, which opens an info
panel with a 3px left border in the same hue. Kinds: `beta` (violet),
`deprecated` (orange), `update` (amber). To add a kind, define one
`--badge-rgb` and nothing else.

Related indicators: `.bt-pair-status` (emerald pill), `.debug-notice-badge`
(amber count bubble), `.settings-update-dot`.

### 3.9 Notification bar

`showNotification(message, type, duration)` from `ui-helpers.js`: a full-width
bar at the top. Types map to `bg-{cyan,emerald,amber,rose}-500/80` for `info`,
`success`, `warning`, `error`. Use it for results of an action; do not build
another toast.

### 3.10 Modal

```html
<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm hidden">
  <div class="w-full max-w-2xl mx-4 max-h-[90vh] flex flex-col bg-zinc-900/50 backdrop-blur-xs border border-zinc-700 rounded-lg shadow-2xl">
```

Header row: `flex justify-between items-center mb-4`, `h3.text-lg.text-white`,
and a close button. Footer actions are right-aligned: secondary (Cancel) then
primary.

Widths: `max-w-md` (short forms, password), `max-w-2xl` (default),
`max-w-3xl` (`.env` editor), `w-96` (memory editor).

On phones the Wi-Fi setup modal becomes a full-screen sheet with an opaque
`#27272a` surface and sticky actions, so nothing shifts when the keyboard
opens. Follow that pattern for multi-step modals on mobile. The live captions
reading view is a native `<dialog>`.

### 3.11 Menu panel

`.menu-panel` is the one menu style: a dark panel (`#142e3b`, `white/14`
border, 12px radius) with transparent `secondary-action` rows, icons and labels
left-aligned. The power menu (`#power-dropdown`) and every `.dropdown-menu`
use it.

```html
<div class="dropdown-menu menu-panel absolute bottom-full right-0 mb-1 w-full z-20 hidden">
  <button type="button" class="secondary-action secondary-action--hover--cyan">
    <span class="material-icons">file_download</span>Export settings
  </button>
</div>
```

`toggleDropdown(btn)` opens the `.dropdown-menu` next to a `.dropdown-toggle`
and rotates its chevron.

### 3.12 Lists and rows

- Status rows: `.system-component-row`, a two-column grid (name | state) with
  `white/7` hairlines between rows; the state colour is set by `data-state`
  (installed = emerald, missing/damaged/outdated/configuration_needed = amber,
  optional = muted: an opt-in component that is not installed).
- Opt-in system components: a `.card-divider` group below the component list,
  one row per component (white name, muted `text-xs` description) with a
  secondary emerald Install button. Never a warning colour: nothing is wrong.
- Definition summary: `.wifi-summary` (`dt` muted, `dd` white).
- Stepper: `.wifi-stepper`, numbered steps with an emerald underline for the
  current step.

### 3.13 Header

`.billy-header` with `.billy-topbar`: logo (48px, 36px on mobile), name
(16px / 650), state line (12px muted with a 7px status dot), then the action
groups. Groups are separated with `.header-separated-actions` (a left hairline).
On phones under 480px the state line moves below the name.

### 3.14 Back pill

The "back" link at the top of the library, songs, persona and profile pages:
`group self-start inline-flex items-center gap-2 rounded-full border border-white/15 bg-zinc-800/60 px-3 py-2 text-sm text-zinc-200 backdrop-blur-xs`
with an `arrow_back` icon at `text-lg` that nudges left on hover.

---

## 4. Extending the system

### 4.1 Before adding anything

1. Look for an existing component in section 3. If one fits roughly, use it
   and adjust layout with utilities. Don't create a per-panel lookalike.
2. If a variant is needed, add a **modifier** to the existing component
   (`.component--variant`), not a new component.
3. A new component is justified when it appears, or will appear, in more than
   one place, or has states (hover, selected, disabled, open).

### 4.2 Writing a component

- Put it in `tailwind/src/input.css` under a comment header
  `/* ===== Component name ===== */`, next to related components.
- Naming: `.block`, `.block-part`, `.block--modifier`.
- Hue variants are driven by RGB variables, as `.status-badge` (`--badge-rgb`),
  the secondary tones (`--tone`) and the filled buttons (`--fill`) do. Don't
  copy whole rule blocks per hue.
- Take colours, radius and spacing from section 1. Use the `--color-*` tokens
  in CSS. A new raw hex value needs a reason.
- Do not style by `#id` for shared looks. The Bluetooth and Wi-Fi blocks still
  do this; it keeps their styles from being reused.
- Every component needs:
  - `:focus-visible` with the canonical focus outline
  - `:disabled` / `[aria-disabled]` with `opacity: .45; cursor: not-allowed`
  - a `[hidden]` rule if the component sets `display`
  - a 44px touch target for anything clickable
  - no animation under `prefers-reduced-motion` and `.reduce-motion`
  - a check at 360px, 768px and 1100px wide
- Run `npm run build:css` in `application/webconfig/` and commit the
  regenerated `static/css/styles.css`. For classes built in JS, check that they
  are in the output.
- Add the component to this document and to `docs/design-system.html`.

### 4.3 Device-specific panels

Hardware variants add their own settings panels through device profiles.
These panels use the same components and tokens as the core UI, so the UI looks
the same whatever hardware it runs on. Panel titles and labels describe the
function ("Phone line", "Display"), not the part inside the device.

### 4.4 Checklist for a change touching the UI

- [ ] Uses existing components; no new near-duplicate.
- [ ] No card inside a card.
- [ ] Colours follow the roles in 1.1.
- [ ] No new arbitrary values (`text-[…]`, `rounded-[…]`, raw hex) without reason.
- [ ] Keyboard focus is visible; icon-only buttons have a name.
- [ ] Works at 360px and on desktop; reduced motion respected.
- [ ] `styles.css` rebuilt; preview and this document updated.

---

## 5. Open items

Not done yet, each can ship on its own:

1. Remove the remaining nested cards: library page, song editor, recovery
   modal, wake-up clips. Each needs its own layout decision.
2. Move the older cards' utility strings to `.card`.
3. Rename `.wf-toggle` to `.toggle`, keeping the old name as an alias for one
   release.
4. Move the `#bluetooth-settings` and Wi-Fi styles from id selectors to
   component classes.
5. Remove the unused templates listed at the top, and the `#settings-panel`
   lookup in `log-panel.js`.
