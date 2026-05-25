# MTG Collection HTML Report — Specification

## Goal

Write a Python script that takes a CSV export of a ManaBox collection and generates a **self-contained interactive HTML report** for quick answers to questions like:

- "Across which binders, and in what quantities, are my blue commons of set TDM distributed?"
- "How many cards do I have from set TLA in total?"
- "How many mythics are in binder X?"

The report is **stateless**: a single HTML file, opened locally in a browser, with no backend and no runtime external requests.

---

## Input

A CSV file in the ManaBox export format. The columns are fixed:

| # | Column | Used | Notes |
|---|--------|------|-------|
| 1 | `Binder Name` | ✅ dimension | string, may contain trailing whitespace and quotes — trim |
| 2 | `Binder Type` | ✅ filter | keep only `binder`, drop `deck` and any others |
| 3 | `Name` | ⚪️ for debugging | not aggregated |
| 4 | `Set code` | ✅ dimension | uppercase-normalize |
| 5 | `Set name` | ⚪️ for UI | human-readable label for Set code |
| 6 | `Collector number` | ❌ | not used |
| 7 | `Foil` | ❌ | not used (in v1) |
| 8 | `Rarity` | ✅ dimension | `common` / `uncommon` / `rare` / `mythic` / `special` — **already in CSV, no enrichment needed** |
| 9 | `Quantity` | ✅ metric | int, summed |
| 10 | `ManaBox ID` | ❌ | not used |
| 11 | `Scryfall ID` | ✅ cache key | card UUID for enrichment via Scryfall |
| 12-17 | others | ❌ | not used |

### Sample rows (after header)

```csv
"Jace Binder",binder,Magus of the Coffers,C14,Commander 2014,148,normal,rare,1,12708,962a16bb-516c-4e74-b8dd-edd4c63c5fff,4.35,false,false,near_mint,en,EUR
"Jace Binder",binder,Grave Titan,C14,Commander 2014,145,normal,mythic,1,12711,68ce4c64-9f82-4be1-aa3b-ba885b2d4307,4.89,false,false,near_mint,en,EUR
"Jace Binder",binder,Ghoulcaller Gisa,C14,Commander 2014,23,normal,mythic,1,12833,bbd9aba4-6db6-417b-8515-5617f0acdc9b,2.22,false,false,near_mint,en,EUR
"\"The Search\" Binder",binder,Elvish Mystic,M15,Magic 2015,170,normal,common,1,...
Tron,deck,Karn Liberated,...   ← must be filtered out (Binder Type=deck)
```

### Scale (on the reference dataset)

- ~8000 rows total, of which ~7400 have `Binder Type=binder`
- ~6100 unique `Scryfall ID`s
- ~210 unique `Set code`s
- dozens of binders

This matters when picking an enrichment strategy (see below) — per-card requests to Scryfall are not acceptable.

---

## Configuration (at the top of the script)

```python
INPUT_CSV = "ManaBox_Collection.csv"
OUTPUT_HTML = "collection_report.html"
BINDER_BLACKLIST = []  # list of Binder Names to exclude from the report; empty by default
SCRYFALL_CACHE_PATH = ".scryfall_cache.json"  # local card-data cache
```

`BINDER_BLACKLIST` — exact match on `Binder Name` after `.strip()`.

---

## Report dimensions

The report visualizes the collection broken down by four dimensions:

1. **Binder** — `Binder Name` (after `.strip()`).
2. **Set code** — `Set code` (uppercase). In the UI, also show `Set name` next to it for convenience.
3. **Rarity** — taken directly from the CSV. Values: `common`, `uncommon`, `rare`, `mythic`, `special`.
4. **Color** — derived from Scryfall data (see below). Buckets:
   - `White`, `Blue`, `Black`, `Red`, `Green` — monocolor
   - `Multicolor` — two or more color identities
   - `Colorless` — artifacts, eldrazi, and anything else with no color identity, **except lands**
   - `Land` — any card with the `Land` type (including basic and nonbasic lands), regardless of color identity. Lands get their own bucket so they don't dilute the others.

**Metric**: always `sum(Quantity)`, not row counts and not a list of specific cards.

---

## Enrichment via Scryfall

### What we need from Scryfall per card

- `color_identity` (array, e.g. `["U"]`, `["W","B"]`, `[]`)
- `type_line` (string, search for the `"Land"` substring)

### Strategy — bulk download, not individual requests

Scryfall publishes bulk data at `https://api.scryfall.com/bulk-data`. Download `default-cards` (or `oracle-cards` if it suffices — but `default-cards` is safer since it contains every printing with concrete Scryfall IDs). This is one large JSON (~500 MB); parse it incrementally, keep only the fields we need for the Scryfall IDs that appear in the CSV, and store them in a local JSON cache `SCRYFALL_CACHE_PATH`.

Algorithm:

1. Read the CSV, collect the set of required `Scryfall ID`s.
2. Load the cache from `SCRYFALL_CACHE_PATH` if it exists.
3. If every required ID is already in the cache — enrichment is done.
4. Otherwise: fetch the URL of the current `default-cards` bulk via `GET https://api.scryfall.com/bulk-data/default-cards`, download the JSON, stream it via `ijson` (or read it line by line if the bulk is a plain array — in which case `json.load` would work, but streaming is preferred), extract the required IDs, append them to the cache, and save.
5. If some ID isn't found in the bulk — fall back to an individual `GET https://api.scryfall.com/cards/{id}` with **at least 100 ms** between requests (Scryfall asks for 50–100 ms). Log such cases. If there are many of them (>50), abort and inform the user.

### Rate-limit compliance and being polite to Scryfall

- A mandatory `User-Agent` header, e.g. `mtg-collection-report/1.0`.
- An `Accept: application/json` header.
- For individual requests — a delay of ≥ 100 ms.
- Network requests only on the first run, or when new cards appear in the CSV that aren't in the cache. On subsequent runs — no network calls.

### Alternatives if Scryfall doesn't work out

If the Scryfall API/bulk becomes unavailable or unsuitable:

- **MTGJSON** (`https://mtgjson.com/`) — `AllPrintings.json` or `AtomicCards.json`, also bulk, also free.
- The script should be structured so that the enrichment source is isolated in a single function/class with the interface "give me `{scryfall_id: {color_identity, type_line}}`", so swapping backends is trivial.

---

## Script pipeline

```
load_csv()           → list[Row] (filter by Binder Type, blacklist, strip Binder Name, Quantity→int)
collect_card_ids()   → set[str]
enrich(ids)          → dict[id → {color_identity, type_line}]   (bulk + cache)
classify_color(card) → one of {White, Blue, Black, Red, Green, Multicolor, Colorless, Land}
aggregate(rows, enriched)
                     → flat list of records
                       [{binder, set_code, set_name, rarity, color, qty}, ...]
                       aggregated by (binder, set_code, rarity, color)
render_html(data)    → a single HTML file with all data inline
```

Intermediate data can be dumped to `report_data.json` for inspection (optional, under a `--debug` flag).

---

## HTML report requirements

### Self-containment

- A single HTML file. Opens with a double-click, no local server.
- **All data inline** in a `<script>` tag as a JS object (a single JSON with a list of records `(binder, set_code, set_name, rarity, color, qty)`).
- JS/CSS dependencies — either inlined or loaded from a CDN (note which in the code). Inlining is preferred, but if a library is heavy (>500 KB) a CDN is acceptable, with an honest comment at the top of the file.

### Visualization

- Each binder is a rectangle (a horizontal stacked bar or a treemap cell), with length = total `qty` in that binder (under the current filters).
- Inside the rectangle — a breakdown by the next dimension (default: Color → Rarity → Set, but the drill-down order must be switchable from the UI).
- Inside each segment, **`qty` is shown as a number directly on the rectangle** (not only in the hover tooltip), and the label matches the current inner breakdown (for a color breakdown — qty by color, for rarity — qty by rarity, etc.). Text color is chosen for contrast against the segment background: a light label on dark buckets, a dark one on light, plus a subtle inverse shadow for readability on top of any fill. If a segment is too narrow to fit the number, the label is hidden (the number stays available via tooltip). The label must not capture mouse events — the hover zone remains the whole segment.
- **Recommendation**: either stacked bars + a side panel with counters, or `d3.hierarchy` + treemap, or Plotly `sunburst`/`treemap`. Plotly gives interactivity out of the box and fits a stateless report well; consider it first. If you pick d3 — OK, but briefly justify it in a comment.

### Design code

A restrained dark theme, readable, no neon acid. Charts on top of the background must be clearly distinguishable — especially the Black bucket, which disappears on a dark background without an outline.

**Background and UI palette**:
- Background base: `#0F1115` (nearly black, but not #000)
- Surface (cards, panels): `#1A1D24`
- Border / divider: `#2A2E37`
- Text primary: `#E6E8EB`
- Text secondary: `#9AA0A6`
- Accent (active filter, hover): `#7AA2F7` (muted blue)

**Color-bucket palette** (adapted for a dark background, MTG recognizability preserved):
- White: `#EDE6C8` — cream, not pure white, so it doesn't burn the eye
- Blue: `#3B82F6` — saturated, but not bright cyan
- Black: `#5B5560` — warm grey-violet; **mandatory thin light outline** (1px `#7A7480`), otherwise it blends into the background
- Red: `#EF4444`
- Green: `#22C55E`
- Multicolor: `#EAB308` — gold
- Colorless: `#94A3B8` — cool grey, distinguishable from Black
- Land: `#A16207` — muted brown

**Principles**:
- No gradients in charts — flat fills only.
- Labels (axis labels, ticks, legend): `#9AA0A6` for secondary, `#E6E8EB` for active.
- Chart grid: `#2A2E37`, thin.
- Hover tooltip: background `#1A1D24`, 1px `#2A2E37` border.
- Font: system sans-serif (`-apple-system, "Segoe UI", system-ui, sans-serif`), no web-font loading.

If Plotly is used — set `template='plotly_dark'` as a starting point and override the palette via `layout.colorway` and `layout.paper_bgcolor`/`plot_bgcolor` to the values above.

### Interactivity (filters)

The UI has independent filters (multi-select) for:

- **Binder** (list from data)
- **Set code** (list from data, label shown as `CODE — Set name`)
- **Rarity** (fixed set: common, uncommon, rare, mythic, special)
- **Color** (fixed set of the 8 buckets above)

Logic: AND between filters, OR within a single filter. Any filter change recomputes aggregates on the fly (on the client, from the inline dataset) and redraws the visualization + summary counters.

### Summary panel

Somewhere near the top of the report — three numbers, recomputed under the current filters:

- Total cards (sum Quantity)
- Distinct cards (count distinct Scryfall ID — to support this, ship a pre-aggregated unique-ID counter on (binder, set, rarity, color) alongside the main aggregate, or simply a second `distinct_count` table next to it)
- Number of binders / sets in the current slice

### Anti-requirements (what we do not do in v1)

- No list of specific cards (aggregates only).
- No prices.
- No foil / non-foil comparison.
- No CSV export from the UI.

These may appear in v2; the code should be structured so they don't require a rewrite (in particular, the record format in the inline dataset is best kept extensible).

---

## Technical requirements for the script

- Python 3.11+.
- Minimal dependencies: standard library + `requests` (for Scryfall) + optionally `ijson` (for streaming the bulk). No pandas for the sake of pandas — `csv` and `collections` are enough, the volumes are small.
- If Plotly is picked for rendering — `plotly` in `requirements.txt`. HTML generation via `plotly.offline.plot(..., include_plotlyjs='inline' or 'cdn', output_type='div')` + manual HTML wrapping with filters.
- CLI:
  ```
  python report.py [--input ManaBox_Collection.csv] [--output report.html] [--refresh-cache] [--debug]
  ```
  - `--refresh-cache` — force re-downloading the bulk even if the cache covers every ID.
  - `--debug` — dump the intermediate `report_data.json`.
- Logging via `logging`, INFO level by default.
- The script is idempotent: a repeat run with no CSV changes and no `--refresh-cache` makes no network requests.

---

## Acceptance criteria

1. Running `python report.py` on the reference CSV completes without errors and produces `collection_report.html`.
2. Opening the HTML in Chrome/Firefox without network shows a working report with all filters.
3. The sum of `Quantity` across all binders in the report with empty filters = `sum(Quantity) over rows where Binder Type='binder' and Binder Name not in BINDER_BLACKLIST`.
4. Filter Set=`TLA` shows the correct total for that set.
5. Filter Color=`Blue` + Rarity=`common` + Set=`TDM` shows exactly the per-binder breakdown of blue commons of TDM — the reference question from the goal.
6. A repeat run of the script with no CSV changes makes no HTTP requests (verified via logs).
7. Adding a new binder to `BINDER_BLACKLIST` and rerunning — the binder disappears from the report, and totals are recomputed.
