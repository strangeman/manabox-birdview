# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

v1 implemented. Repo contents:

- `report.py` — the script (~1000 LOC, single file)
- `spec.md` — full specification (English); authoritative when behavior is unclear
- `requirements.txt` — `requests`, `ijson`
- `.scryfall_cache.json` — populated card-data cache (gitignored)
- `examples/collection_report.html` — generated reference report
- `.gitignore` — excludes cache, generated HTML, CSV, `__pycache__`

Run: `python report.py` (uses defaults from the spec). All seven acceptance criteria in `spec.md` are satisfied as of the last run. When in doubt about pipeline behavior, `spec.md` is authoritative — read it before changing things.

## What is being built

A single Python script (`report.py`, Python 3.11+) that ingests a ManaBox CSV export of a Magic: The Gathering collection and emits a **self-contained, stateless HTML report** (`collection_report.html`) — opens via double-click, no server, no runtime network calls. The report answers questions like "how many blue commons of set TDM are in each binder?" via on-the-fly client-side filtering over an inline JSON dataset.

Planned CLI:
```
python report.py [--input ManaBox_Collection.csv] [--output report.html] [--refresh-cache] [--debug]
```
- `--refresh-cache` forces a fresh Scryfall bulk download even if the cache covers all IDs.
- `--debug` dumps intermediate `report_data.json`.

## Architecture (pipeline)

```
load_csv()           → filter Binder Type=='binder', drop blacklisted binders,
                       strip Binder Name, Quantity→int
collect_card_ids()   → set of Scryfall IDs needed
enrich(ids)          → {scryfall_id: {color_identity, type_line}} via bulk + cache
classify_color(card) → one of {White, Blue, Black, Red, Green, Multicolor, Colorless, Land}
aggregate()          → flat records [{binder, set_code, set_name, rarity, color, qty}, ...]
                       grouped by (binder, set_code, rarity, color)
render_html(data)    → one HTML file with all data inline
```

Data model is **always aggregated by `sum(Quantity)`** — never row counts, never per-card lists (v1 anti-requirement).

### Critical invariants

- **`Binder Type` filter**: only rows where `Binder Type == 'binder'` go into the report. `deck` and others are dropped at ingest.
- **`Binder Name` normalization**: `.strip()` before any comparison, keying, or blacklist lookup. Source CSV may contain trailing whitespace and embedded escaped quotes.
- **`Set code`**: uppercase-normalize.
- **`Rarity`**: taken verbatim from CSV (`common` / `uncommon` / `rare` / `mythic` / `special`) — do **not** re-derive from Scryfall.
- **Color bucketing** (derived from Scryfall, mutually exclusive, in this priority order):
  1. `Land` if `type_line` contains `"Land"` — overrides color identity, even for colored lands.
  2. `Colorless` if `color_identity == []` and not a land.
  3. `Multicolor` if `len(color_identity) >= 2`.
  4. Mono `White`/`Blue`/`Black`/`Red`/`Green` for single-element color identities.

### Scryfall enrichment — must be bulk, not per-card

Reference dataset has ~6100 unique Scryfall IDs. Per-card requests are explicitly forbidden as the primary path.

1. Read CSV, collect needed Scryfall IDs.
2. Load `.scryfall_cache.json` if present; if it covers all needed IDs (and `--refresh-cache` not passed), skip the network entirely.
3. Otherwise: `GET https://api.scryfall.com/bulk-data/default-cards` → fetch the bulk URL → stream-parse (prefer `ijson` for the ~500MB JSON), keep only `color_identity` and `type_line` for needed IDs, merge into cache, persist.
4. Fallback for missing IDs: individual `GET https://api.scryfall.com/cards/{id}` with **≥100 ms** delay between requests. Abort with a clear error if >50 IDs need this fallback.

Required HTTP headers on every Scryfall call: `User-Agent: mtg-collection-report/1.0`, `Accept: application/json`.

**Idempotence requirement (acceptance criterion #6)**: a second run with unchanged CSV and no `--refresh-cache` must perform zero HTTP requests. Verify via logs when changing enrichment code.

The enrichment source must stay isolated behind an interface returning `{scryfall_id: {color_identity, type_line}}`, so swapping Scryfall for MTGJSON (`AllPrintings.json` / `AtomicCards.json`) stays a one-function change.

## HTML report constraints

- **One file**, all data inline as a JS object inside `<script>`. Open via double-click — no server, no runtime fetches.
- **Filters** (multi-select, AND across filters, OR within a filter): Binder, Set code (label `CODE — Set name`), Rarity, Color. Filter changes recompute aggregates client-side from the inline dataset.
- **Summary panel** (recomputed under filters): total cards (sum Quantity), distinct cards (count distinct Scryfall ID — requires shipping a `distinct_count` view alongside the qty aggregate), binder/set counts in the current slice.
- **Visualization**: each binder is a rectangle sized by total qty under current filters, with internal breakdown by switchable drill-down (default Color → Rarity → Set). Plotly (`treemap`/`sunburst`/stacked bars) is the recommended first choice for stateless interactivity; d3 is allowed with a brief justification comment. If Plotly: start from `template='plotly_dark'` and override the palette below.
- **Plotly inlining**: `plotly.offline.plot(..., include_plotlyjs='inline', output_type='div')` is preferred. CDN is acceptable only with an explicit comment if the inlined size is prohibitive (>500 KB threshold per spec).

### Design tokens (dark theme — must match exactly)

Background `#0F1115` · Surface `#1A1D24` · Border `#2A2E37` · Text primary `#E6E8EB` · Text secondary `#9AA0A6` · Accent `#7AA2F7`.

Color buckets:
- White `#EDE6C8` · Blue `#3B82F6` · **Black `#5B5560` with mandatory 1px `#7A7480` outline** (otherwise it disappears on the dark surface) · Red `#EF4444` · Green `#22C55E` · Multicolor `#EAB308` · Colorless `#94A3B8` · Land `#A16207`.

No gradients in chart fills. System sans-serif only (`-apple-system, "Segoe UI", system-ui, sans-serif`) — no web font loads.

## Configuration (top of script)

```python
INPUT_CSV = "ManaBox_Collection.csv"
OUTPUT_HTML = "collection_report.html"
BINDER_BLACKLIST = []                     # exact match on stripped Binder Name
SCRYFALL_CACHE_PATH = ".scryfall_cache.json"
```

`BINDER_BLACKLIST` is exact-match against `Binder Name.strip()`.

## Acceptance criteria (from spec — verify before declaring done)

1. `python report.py` on the reference CSV produces `collection_report.html` without error.
2. The HTML works in Chrome/Firefox with no network.
3. Total Quantity with empty filters == `sum(Quantity)` over rows where `Binder Type=='binder'` and `Binder Name` not in `BINDER_BLACKLIST`.
4. Filter Set=`TLA` shows the correct total for that set.
5. Filter combo Color=`Blue` + Rarity=`common` + Set=`TDM` returns the per-binder breakdown of blue commons of TDM (the headline reference query).
6. A second run with unchanged CSV and no `--refresh-cache` makes zero HTTP requests.
7. Adding a binder to `BINDER_BLACKLIST` and re-running removes it from the report and recomputes totals.

## v1 anti-requirements (do not implement)

No per-card listings, no prices, no foil/non-foil split, no CSV export from the UI. Keep the inline record format extensible so v2 can add these without a rewrite.

## Dependencies

Stay minimal: stdlib + `requests` + optional `ijson` (recommended for streaming the Scryfall bulk). `plotly` if used for rendering. **No pandas** — `csv` and `collections` are sufficient for this volume.
