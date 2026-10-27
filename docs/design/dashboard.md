# Design brief: the Praxis decision console (Phase 13)

Written before any UI code. Every later choice in `frontend/` should trace back to a line in
this file; if it cannot, it is decoration and gets cut.

## Point of view

An instrument for one operator who has to decide something: is the system healthy, is a
number trustworthy, should this request be approved. It is calm and precise. It reads like
an instrument panel or a well-set statistical table, not a marketing page. Nothing on the
screen competes for attention it has not earned (the principle Linear states for its own
refresh); the data and its trust state are the only loud things.

Three questions every panel answers, in this order:

1. What is the value, in what unit, compared with what?
2. Can I trust it? (source, age, synthetic or external, stale, missing and why)
3. What can I do about it, and who is allowed to?

## What we took from prior work (principles, not visuals)

| Source | Principle we use |
| --- | --- |
| Stripe Dashboard | Hierarchy from type and whitespace, not colour; a number is always paired with its comparison period; the time of the last data is printed beside the data. |
| Linear | Dense but not cluttered; dimmed navigation so the work area leads; few icons. |
| Grafana / Datadog | A gap in data is a gap, never a zero; panels survive a failed query on their own. Their failure mode (a blank "No data" with no reason) is the thing we avoid. |
| Bloomberg Terminal | Every figure carries its unit and timestamp; fixed positions so the eye learns where things are. |
| FT / Economist charts | Titles say what the chart shows; few ticks; the latest value is labelled at the line's end; no chart junk; uncertainty is drawn, not footnoted. |
| ONS chart guidance | Gridlines behind the data, no borders or backgrounds around charts. |
| Ramp / Mercury | Money is set in tabular figures with the currency in the label, never in the number's colour. |

## Type

* **Newsreader** (Production Type, optical sizes) for view titles and the one-line deck under
  each title only. It is the editorial voice: one serif, used rarely.
* **Inter** (variable, optical sizes) for everything else: labels, body, and every number.
  It was drawn for screens, has true tabular figures (`tnum`), and `cv11` gives a single-storey
  a. It replaced IBM Plex Sans after review: Plex at 11 to 13 px read too thin.
* **IBM Plex Mono** only for identifiers that are code (`dec-…`, `inc_…`, call ids, hashes,
  model versions). Never for ordinary labels.
* Numbers: `font-variant-numeric: tabular-nums` in every column, table, axis tick and readout,
  so digits align. The single hero figure of a view uses proportional figures (dataviz rule).
* Scale (px): 12 / 14 / 16, then 18.7 / 22.5 / 27 / 32.4. Titles 32.4 serif; panel titles 16
  Inter 600; body and labels 14 / 400 in the secondary ink; readout values 22.5. Nothing is
  smaller than 12 px. No `-webkit-font-smoothing: antialiased` (it thins the strokes).
* Sentence case everywhere. Upper case only for machine states that the operator must not
  misread: `STALE 3h 12m`, `SYNTHETIC`, `NO DATA`, `SHADOW`, `PENDING`. These get a 0.04em
  letter-spacing and the 12px size, never more.
* Line length ≤ 72 characters for prose (notes, reasons).

## Colour tokens

One accent. Four semantic colours: ok, degraded, stale, synthetic. Nothing else carries hue.
There are no gradients, glows, blur or shadows.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#f3f4f5` | `#0e1114` | page |
| `--surface` | `#ffffff` | `#161b20` | panels, charts |
| `--ink` | `#15191d` | `#e7eaed` | primary text, values |
| `--ink-2` | `#4a525b` | `#aab2bb` | labels, secondary text |
| `--ink-3` | `#6c747e` | `#8a939d` | axis ticks, meta |
| `--rule` | `#d8dce0` | `#2a3138` | hairlines, gridlines |
| `--accent` | `#008f88` | `#1fa39c` | marks: the series of interest, focus ring, the active nav item |
| `--accent-ink` | `#00706b` | `#4cc2bb` | accent used as text (≥ 4.5:1) |
| `--ok` | `#2e7d4f` | `#5cb27e` | healthy state text and dot |
| `--degraded` | `#b4690a` | `#c8851a` | firing alert, failing check, refused action |
| `--stale` | `#5b6b7f` | `#91a0b3` | stale or missing input; the value is dimmed and its badge says why |
| `--synthetic` | `#7a6a3a` | `#c2b07a` | the SYNTHETIC tag |

Chart marks: the series of interest in `--accent`; a comparison or baseline in `--ink-3`;
uncertainty bands in `--accent` at 14% opacity. Accent vs degraded was validated with the
dataviz palette validator (light on `#ffffff`, dark on `#161b20`): all checks pass. Text never
takes a series colour; a coloured mark sits beside the text instead.

The semantic colours never appear as large fills. A state is a short word plus a 6px dot,
both in the state colour; the rest of the panel stays neutral. A whole page turning amber is
exactly the alarm-colour look this brief rejects.

## Space, grid, density

* 4px base. Steps: 4, 8, 12, 16, 24, 32, 48.
* Desktop: a 208px navigation rail (dimmed ink) plus a content column on a 12-column grid, 24px
  gutters, max content width 1280px. At 390px the rail becomes a top row of view links and
  every grid collapses to one column. No horizontal page scroll; wide tables scroll inside
  their own panel.
* Panels are separated by hairline rules (1px `--rule`) and whitespace, not by boxes. Only two
  things get a border: the decision detail drawer and the approval form, because they are
  objects you act on.
* Radius: 2px on inputs and buttons, 0 elsewhere. No shadows.
* Density: table rows 32px, readout cells 72px. Generous outer margins, tight inner spacing.

## Readouts, not KPI cards

The head of each view is a **readout**: one row of figures separated by vertical hairlines,
each with a sentence-case label, the value, its unit, a comparison ("vs 28-day median",
"80% interval"), an optional 28-point sparkline, and a trust line (source and age). No icons,
no coloured circles, no card shadows, and never more than six figures in a row.

## Trust states (the core of the design)

| State | How it reads |
| --- | --- |
| fresh | the age in `--ink-3` ("read 14:02:11 UTC, 12 s old") |
| stale | `STALE 3h 12m` in `--stale` with a dot; the value is set in `--ink-3`, not hidden |
| no data | `NO DATA` plus the reason in a sentence ("Pub/Sub emulator has no backlog API"); never a 0 or an em dash alone |
| synthetic | `SYNTHETIC` tag in `--synthetic` at the head of every view and on every figure that is simulator output |
| external | source name plus retrieval time (provenance) |
| shadow | `SHADOW` tag plus "not validated for execution" on every pricing figure |
| error | the panel says what failed, prints the correlation id, offers retry; never a stack trace |

## Chart grammar

* Hand-built SVG; one y-axis per chart; never dual-axis; no pie or donut charts.
* 3 to 5 y ticks on clean numbers, compact from 1,000 (1K, 2K); x ticks at day boundaries.
* Gridlines horizontal only, solid 1px `--rule`, behind data. No vertical gridlines, no
  borders around the plot.
* Lines 2px with round caps; the end value labelled at the line's end in `--ink`; an 8px end
  dot with a 2px surface ring.
* Uncertainty: a filled band (p05 to p95, or the 80% interval) under the line, labelled once in
  the chart's subtitle. A point estimate without an interval says "no interval recorded".
* Annotations: a single vertical rule plus a short label for events that explain the data
  (current price, chosen price, champion promotion). No arrows.
* Units in the subtitle ("GBP per day, simulated days"), not on every tick.
* Missing days render as gaps in the line, never interpolated.
* Every chart has a table equivalent (a disclosure under it) and a hover readout that shows
  the same values; the tooltip never gates a value.
* Small multiples (same scale) to compare products or models; never stacked colours.

## Motion

Only functional motion: a 120 ms opacity change when data refreshes (the previous render is
held at 60% opacity until the new data arrives, so the layout never jumps), and a 160 ms
height change when a disclosure opens. `prefers-reduced-motion` turns both off.

## Copy

Plain, specific, in the interface's voice. Buttons say exactly what happens ("Approve freeze",
"Reject request", "Lift freeze"). Errors say what failed and what to do. Empty states say why
they are empty and which command fills them. The navigation names views by the operator's
question, and the URL by the plan's view name.

## Explicitly rejected

Purple or blue gradients, glassmorphism, glows, neon, blurred blobs, emoji, an icon beside
every heading, rows of identical rounded KPI cards with an icon in a coloured circle, a
"Welcome back" hero, Inter-on-white-with-shadows, untouched component-library defaults,
lorem ipsum or invented numbers, rounded-2xl everything, chart junk, pie or donut charts for
time series, rainbow categorical palettes, alarm colours across whole panels.
