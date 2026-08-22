# nobrainr Dashboard — React Port Plan

> Status: **PROPOSED** · Author: fable-main (bimavo) · Date: 2026-08-21
> Decision owner: Victor. Nothing in this document is committed work until the
> tracking issue's Wave 0 is approved.

## 0. Why (and why not)

**For:**
- **Stack alignment.** bimavo is mid-port to React (`refactor/react-main`,
  PORT_100 wave process). One frontend stack across products means shared
  habits, shared review instincts, and eventually shared components
  (theming, data-grid, panels).
- **AI-assisted velocity.** Agent output quality in the React ecosystem is
  measurably better in practice — richer training distribution, more
  first-class libraries for the patterns this app needs (TanStack Query for
  the polling this app hand-rolls in every view, framer-motion for the
  reveal choreography).
- **Live-data hygiene.** Today every view implements its own
  `setInterval` + `ref` polling with its own countdown. TanStack Query
  replaces all of it with declarative `refetchInterval`, cache
  de-duplication, and window-focus refetch — that alone deletes ~400 lines
  and several classes of stale-state bug.

**Against (stated honestly):**
- The hard views are **imperative canvas islands** (pixi+d3, three.js,
  cosmos). React does not improve them; they are framework-agnostic and
  port nearly unchanged. Anyone expecting rendering gains from React alone
  will be disappointed — the gains come from consolidation done *alongside*
  the port.
- A working 15.5k-LOC app gets zero user-visible value from a rewrite until
  the last wave lands. The mitigation is the strangler approach below —
  every wave ships a usable page.

## 1. Inventory (measured, not estimated)

| unit | count | notes |
|---|---|---|
| views | 13 | `src/views/*.vue` |
| components | 13 | AppBar, CommandPalette, etc. |
| total LOC | 15,482 | views + components |
| pinia stores | 2 | small |
| Vuetify usage | **1 of 13 views** | the rest are hand-rolled CSS — ports mechanically |
| canvas islands | 4 | Constellation (pixi+d3), Graph (sigma), Galaxy (cosmos/three), Timeline (partial) |

### Per-view sizing

| view | LOC-class | canvas? | port class | est. sessions |
|---|---|---|---|---|
| ScriptoriumView | S | no | mechanical | 0.5 |
| PulseView | M | no | mechanical | 1 |
| InsightsView | M | no | mechanical | 1 |
| SchedulerView | M | no | mechanical | 1 |
| TimelineView | M | partial | mechanical+ | 1.5 |
| CommonplaceView | M | no | mechanical | 1 |
| LibraryView | M | no | **absorbed into Memories** (see §2) | 0 |
| MemoriesView | L | no | mechanical + absorb Library | 2 |
| ThreadsView + ThreadDetail | M | no | mechanical | 1.5 |
| GraphView | L | sigma | **absorbed into Constellarium** (see §2) | 0 |
| ConstellationView | L | pixi+d3 | island transplant | 2 |
| GalaxyView | L | cosmos | island transplant | 1.5 |
| AppBar + CommandPalette + shell | M | no | rebuild (router/nav idioms differ) | 2 |

**Total: ~15 focused sessions of porting + ~5 of consolidation, QA and
gates ≈ 20–30 sessions, 15–25 PRs.** Calendar: 1–2 weeks of supervised
part-time work. This tracks the bimavo port's observed per-wave velocity.

## 2. Pre-port consolidation (do FIRST — shrinks the surface)

1. **Graph unification** (`#148`-class work): GraphView (sigma + graphology
   + ForceAtlas2) and ConstellationView (pixi + d3-force) become ONE view
   with Obsidian's own model: `global` mode (current Constellarium) and
   `local` mode (seek an entity → n-hop neighbourhood, depth slider).
   Deletes the third graph stack and the 223kB sigma chunk. 1–2 sessions.
2. **Library → Memories**: Library is a *source filter* on the Memories
   treatment ("memories | documents"), not its own page. The `/api/library`
   payload maps onto the Memories list contract with a `kind` badge.
   1 session.

After consolidation the port surface is **11 views**, two of them already
the newest code in the repo.

## 3. Target stack

| concern | choice | rationale |
|---|---|---|
| build | Vite + React 19 + TypeScript | same tooling family as today; bimavo parity |
| routing | react-router v7 | file of routes, lazy chunks per view (same chunk strategy as now) |
| server state | **TanStack Query** | replaces every hand-rolled poller; `refetchInterval` per view |
| client state | zustand (2 tiny stores) | pinia equivalence without ceremony |
| styling | hand-rolled CSS modules, tokens preserved **verbatim** | the manuscript identity (`--cp-gold #c8a96e`, Georgia serif, quire/folio vocabulary) is the product; no Tailwind conversion — that WOULD be a redesign, and it is explicitly out of scope |
| canvas islands | unchanged libs (pixi, d3, cosmos, three) inside `useEffect` + refs | framework-agnostic; transplant, don't rewrite |
| motion | CSS-first; framer-motion only where Vue `motion-v` is used today | parity, not embellishment |

## 4. Wave plan (strangler; every wave leaves a working app)

Both apps run side by side during the port: React app mounts under
`/next/*` behind the same nginx, sharing `/api/*`. A wave = port + gate +
flip the nav link for that view. Rollback per view = flip the link back.

- **Wave 0 — skeleton + contract tests.** Vite app, router, tokens file,
  API client, TanStack Query defaults; **golden JSON fixtures** captured
  from every `/api/*` endpoint the views consume (the parity oracle).
- **Wave 1 — Scriptorium + Pulse.** Newest, cleanest, no canvas. Proves
  tokens, polling idiom, and the gate mechanics end to end.
- **Wave 2 — ledger views.** Insights, Scheduler, Commonplace, Threads(+Detail).
- **Wave 3 — Memories (+ absorbed Library).** The richest non-canvas view.
- **Wave 4 — canvas islands.** Constellarium (unified graph), Galaxy,
  Timeline. Transplants: the imperative core files move with minimal edits;
  only the lifecycle wrapper changes.
- **Wave 5 — shell.** AppBar, CommandPalette, route flip `/next/*` → `/*`,
  Vue app removed. Keep the Vue build deployable for one release as the
  rollback.

## 5. Verification plan (the part that makes this safe)

Per-wave gates, all scriptable, none aspirational:

1. **Contract gate** — golden fixtures from Wave 0 replayed against the
   React view's data layer: every field the Vue view rendered must be
   consumed or explicitly waived in the PR description.
2. **Visual gate** — Playwright (via the crawl4ai container, pattern
   established 2026-08-21: launch with
   `executable_path=…/chromium-1223/chrome-linux64/chrome`, hit the
   dashboard container directly to bypass the VPN-only middleware, dismiss
   the first-visit codex modal) screenshots Vue view vs React view at
   1440×900 and 390×844; pixel-diff ≤ 2% excluding live-data regions.
3. **Interaction gate (canvas views)** — scripted pointer scenarios, the
   same probe suite used for the Obsidian-parity fix: hover fade engages
   and releases, node drag moves THE NODE not the stage, wheel zoom fades
   labels in, pan works on empty space, seek centers. Each asserts on
   DOM/state side-effects (folio panel content), not just "no error".
4. **Console gate** — zero `error`-level console messages during the
   scenario run.
5. **Perf gate** — first-render ≤ Vue baseline +10%; canvas views hold
   ≥ 30 FPS during a 10s scripted interaction storm on the deploy box.
6. **Bundle gate** — per-route chunk ≤ Vue equivalent +15% (TanStack Query
   pays for itself by deleting poller code).

CI: gates 1, 4, 6 in GitHub Actions on every PR; gates 2, 3, 5 run on the
deploy box via the crawl4ai probe harness, results pasted into the PR.

## 6. Risks

| risk | mitigation |
|---|---|
| canvas islands regress subtly during transplant | islands move file-whole; interaction gate is mandatory for them |
| polling → Query changes timing visible to users | keep each view's existing interval as its `refetchInterval` |
| the manuscript identity drifts in translation | tokens copied verbatim to one `tokens.css`; visual gate on every wave |
| port stalls half-done | strangler: any pause leaves a fully working mixed app; nav flips are per-view |
| two apps double the deploy surface temporarily | one nginx, one image, `/next/*` subpath — single deploy unit throughout |

## 7. Explicit non-goals

- No redesign. Pixel-parity is the success criterion; improvements are
  separate PRs after the port.
- No Vuetify-equivalent component library. 1 of 13 views uses it; that one
  view gets hand-rolled equivalents.
- No SSR/Next.js. This is a VPN-only operator dashboard; an SPA is correct.
