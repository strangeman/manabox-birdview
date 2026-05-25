#!/usr/bin/env python3
"""Generate a self-contained HTML report from a ManaBox CSV export.

See spec.md for the full specification (in Russian). High-level pipeline:

    load_csv → collect_card_ids → enrich (Scryfall bulk + cache)
             → classify_color → aggregate → render_html
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import ijson  # type: ignore

    HAS_IJSON = True
except ImportError:
    HAS_IJSON = False


# ---- Configuration (top of script per spec) ---------------------------------

INPUT_CSV = "ManaBox_Collection.csv"
OUTPUT_HTML = "collection_report.html"
BINDER_BLACKLIST: list[str] = []  # exact match against Binder Name after .strip()
SCRYFALL_CACHE_PATH = ".scryfall_cache.json"


# ---- Constants --------------------------------------------------------------

USER_AGENT = "mtg-collection-report/1.0"
SCRYFALL_BULK_META_URL = "https://api.scryfall.com/bulk-data/default-cards"
SCRYFALL_CARD_URL = "https://api.scryfall.com/cards/{id}"
INDIVIDUAL_REQUEST_DELAY_S = 0.1
MAX_INDIVIDUAL_FALLBACK = 50

RARITIES = ["common", "uncommon", "rare", "mythic", "special"]
COLOR_ORDER = [
    "White", "Blue", "Black", "Red", "Green",
    "Multicolor", "Colorless", "Land",
]

COLOR_PALETTE = {
    "White": "#EDE6C8",
    "Blue": "#3B82F6",
    "Black": "#5B5560",
    "Red": "#EF4444",
    "Green": "#22C55E",
    "Multicolor": "#EAB308",
    "Colorless": "#94A3B8",
    "Land": "#A16207",
}

# Picked to read on the dark surface; not specified in the spec.
RARITY_PALETTE = {
    "common": "#6B7280",
    "uncommon": "#B0BEC5",
    "rare": "#D4A437",
    "mythic": "#E0654A",
    "special": "#A78BFA",
}


# ---- CSV ingest -------------------------------------------------------------

def load_csv(path: Path, blacklist: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    skipped = defaultdict(int)
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if (r.get("Binder Type") or "").strip().lower() != "binder":
                skipped["type"] += 1
                continue
            binder = (r.get("Binder Name") or "").strip()
            if binder in blacklist:
                skipped["blacklist"] += 1
                continue
            try:
                qty = int(r.get("Quantity") or 0)
            except ValueError:
                skipped["qty_parse"] += 1
                continue
            if qty <= 0:
                skipped["qty_zero"] += 1
                continue
            sid = (r.get("Scryfall ID") or "").strip()
            if not sid:
                skipped["no_id"] += 1
                continue
            rows.append({
                "binder": binder,
                "set_code": (r.get("Set code") or "").strip().upper(),
                "set_name": (r.get("Set name") or "").strip(),
                "rarity": (r.get("Rarity") or "").strip().lower(),
                "qty": qty,
                "scryfall_id": sid,
            })
    logging.info(
        "Loaded %d binder rows. Skipped: %s",
        len(rows), dict(skipped) or "none",
    )
    return rows


def collect_card_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {r["scryfall_id"] for r in rows}


# ---- Scryfall enrichment ----------------------------------------------------

def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.warning("Cache at %s is unreadable (%s); ignoring.", path, e)
        return {}
    if not isinstance(data, dict):
        logging.warning("Cache at %s is not a dict; ignoring.", path)
        return {}
    return data


def save_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    tmp.replace(path)


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    return s


def _fetch_bulk_url(session: requests.Session) -> str:
    logging.info("Resolving bulk-data URL via %s", SCRYFALL_BULK_META_URL)
    r = session.get(SCRYFALL_BULK_META_URL, timeout=30)
    r.raise_for_status()
    payload = r.json()
    url = payload.get("download_uri")
    if not url:
        raise RuntimeError(f"No download_uri in bulk-data response: {payload}")
    logging.info(
        "Bulk URI: %s (size ~%s bytes, updated %s)",
        url, payload.get("size", "?"), payload.get("updated_at", "?"),
    )
    return url


def _stream_bulk_to_file(session: requests.Session, url: str, dest: Path) -> None:
    logging.info("Downloading bulk → %s", dest)
    with session.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                total += len(chunk)
        logging.info("Downloaded %d bytes (%.1f MB)", total, total / 1024 / 1024)


def _extract_from_bulk(path: Path, needed: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with open(path, "rb") as f:
        if HAS_IJSON:
            for card in ijson.items(f, "item"):
                cid = card.get("id")
                if cid in needed and cid not in out:
                    out[cid] = {
                        "color_identity": list(card.get("color_identity") or []),
                        "type_line": card.get("type_line") or "",
                    }
                    if len(out) == len(needed):
                        break
        else:
            logging.warning(
                "ijson not installed; loading entire bulk into memory. "
                "`pip install ijson` for streaming."
            )
            data = json.load(f)
            for card in data:
                cid = card.get("id")
                if cid in needed:
                    out[cid] = {
                        "color_identity": list(card.get("color_identity") or []),
                        "type_line": card.get("type_line") or "",
                    }
    return out


def _fetch_individual(
    session: requests.Session, ids: list[str]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cid in ids:
        time.sleep(INDIVIDUAL_REQUEST_DELAY_S)
        url = SCRYFALL_CARD_URL.format(id=cid)
        r = session.get(url, timeout=30)
        if r.status_code == 404:
            logging.warning("Scryfall ID %s not found via individual lookup", cid)
            continue
        r.raise_for_status()
        card = r.json()
        out[cid] = {
            "color_identity": list(card.get("color_identity") or []),
            "type_line": card.get("type_line") or "",
        }
    return out


def enrich(
    needed: set[str], cache_path: Path, refresh: bool,
) -> dict[str, dict[str, Any]]:
    cache = load_cache(cache_path)
    if not refresh:
        missing = needed - cache.keys()
        if not missing:
            logging.info(
                "All %d Scryfall IDs already in cache; no network calls.",
                len(needed),
            )
            return {k: cache[k] for k in needed}
        logging.info(
            "Cache covers %d/%d IDs; %d missing.",
            len(needed) - len(missing), len(needed), len(missing),
        )
    else:
        missing = set(needed)
        logging.info("--refresh-cache: refetching all %d IDs.", len(missing))

    session = _make_session()
    bulk_url = _fetch_bulk_url(session)
    bulk_path = cache_path.parent / ".scryfall_bulk.tmp.json"
    try:
        _stream_bulk_to_file(session, bulk_url, bulk_path)
        found = _extract_from_bulk(bulk_path, missing)
    finally:
        try:
            bulk_path.unlink()
        except FileNotFoundError:
            pass
    logging.info("Bulk yielded %d/%d needed IDs.", len(found), len(missing))
    cache.update(found)

    still_missing = missing - found.keys()
    if still_missing:
        if len(still_missing) > MAX_INDIVIDUAL_FALLBACK:
            save_cache(cache_path, cache)
            raise RuntimeError(
                f"{len(still_missing)} IDs not in bulk; exceeds fallback limit "
                f"of {MAX_INDIVIDUAL_FALLBACK}. Aborting (cache partially saved)."
            )
        logging.info("Falling back to %d individual lookups.", len(still_missing))
        cache.update(_fetch_individual(session, sorted(still_missing)))

    save_cache(cache_path, cache)
    return {k: cache[k] for k in needed if k in cache}


# ---- Color classification ---------------------------------------------------

_MONO = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}


def classify_color(card: dict[str, Any]) -> str:
    type_line = card.get("type_line") or ""
    if "Land" in type_line:
        return "Land"
    ci = card.get("color_identity") or []
    if not ci:
        return "Colorless"
    if len(ci) >= 2:
        return "Multicolor"
    return _MONO.get(ci[0], "Colorless")


# ---- Aggregation ------------------------------------------------------------

def aggregate(
    rows: list[dict[str, Any]],
    enriched: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Aggregate rows by (binder, scryfall_id).

    Spec asks for aggregation by (binder, set, rarity, color), but keeping
    scryfall_id in the records is the only way to make `distinct cards under
    current filters` correct in the browser (the spec acknowledges this
    limitation in the summary-panel section). The 4-tuple aggregate is then
    rebuilt client-side from these rows on every filter change.
    """
    bucket: dict[tuple[str, str], dict[str, Any]] = {}
    set_names: dict[str, str] = {}
    missing_enrichment = 0

    for r in rows:
        sid = r["scryfall_id"]
        card = enriched.get(sid)
        if not card:
            missing_enrichment += 1
            continue
        color = classify_color(card)
        key = (r["binder"], sid)
        if key not in bucket:
            bucket[key] = {
                "binder": r["binder"],
                "set_code": r["set_code"],
                "set_name": r["set_name"],
                "rarity": r["rarity"],
                "color": color,
                "scryfall_id": sid,
                "qty": 0,
            }
        bucket[key]["qty"] += r["qty"]
        if r["set_code"] and r["set_code"] not in set_names:
            set_names[r["set_code"]] = r["set_name"]

    if missing_enrichment:
        logging.warning(
            "%d rows had no Scryfall data and were dropped.", missing_enrichment,
        )

    return list(bucket.values()), set_names


# ---- HTML rendering ---------------------------------------------------------

# Visualization choice: vanilla SVG/HTML rather than Plotly. The spec
# recommends Plotly first, but inlining plotly.js (~3 MB) bloats the output
# and the chart we need (per-binder horizontal stacked bars with switchable
# drill-down) is small enough to build directly. Keeps the report a compact
# single file with no CDN dependency, satisfying "open offline in a browser".

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MTG Collection Report</title>
<style>
:root {
  --bg: #0F1115;
  --surface: #1A1D24;
  --border: #2A2E37;
  --text: #E6E8EB;
  --text2: #9AA0A6;
  --accent: #7AA2F7;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
}
header {
  padding: 12px 24px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  display: flex;
  align-items: baseline;
  gap: 16px;
}
header h1 { margin: 0; font-size: 17px; font-weight: 600; }
header .subtitle { color: var(--text2); font-size: 12px; }
.layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  height: calc(100vh - 47px);
}
.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  padding: 14px;
  overflow-y: auto;
}
.summary {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px;
  margin-bottom: 16px;
}
.stat {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
}
.stat .label {
  color: var(--text2);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.stat .value {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.filter-group { margin-bottom: 14px; }
.filter-group .title {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 4px;
}
.filter-group h3 {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--text2);
  margin: 0;
  font-weight: 600;
  letter-spacing: 0.05em;
}
.filter-actions { font-size: 11px; }
.filter-actions span {
  color: var(--accent);
  cursor: pointer;
  margin-left: 8px;
}
.filter-actions span:hover { text-decoration: underline; }
.filter-list {
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  padding: 2px 0;
}
.filter-list label {
  display: flex;
  align-items: center;
  padding: 3px 8px;
  cursor: pointer;
  font-size: 13px;
  user-select: none;
  line-height: 1.2;
}
.filter-list label:hover { background: var(--surface); }
.filter-list input[type=checkbox] {
  margin: 0 8px 0 0;
  accent-color: var(--accent);
  flex-shrink: 0;
}
.filter-list .lbl {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 6px;
  border-radius: 2px;
  flex-shrink: 0;
}
.swatch.outline { box-shadow: inset 0 0 0 1px #7A7480; }
.main {
  padding: 16px 24px;
  overflow-y: auto;
}
.controls {
  margin-bottom: 14px;
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.controls label { color: var(--text2); font-size: 13px; }
.controls select {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 8px;
  font: inherit;
  margin-left: 6px;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--text2);
}
.legend .item { display: flex; align-items: center; gap: 4px; }
.binder-row {
  display: grid;
  grid-template-columns: 240px 1fr 80px;
  gap: 12px;
  align-items: center;
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
}
.binder-name {
  color: var(--text);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-wrap { display: block; }
.binder-bar {
  display: flex;
  height: 22px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 2px;
  overflow: hidden;
}
.seg {
  height: 100%;
  cursor: pointer;
  transition: filter 0.08s;
  position: relative;
  overflow: hidden;
}
.seg:hover { filter: brightness(1.18); }
.seg.outline-black { box-shadow: inset 0 0 0 1px #7A7480; }
.seg-label {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  pointer-events: none;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1;
  white-space: nowrap;
  padding: 0 3px;
}
.seg-label.on-dark { color: #FFFFFF; text-shadow: 0 0 2px rgba(0,0,0,0.55); }
.seg-label.on-light { color: #15171C; text-shadow: 0 0 2px rgba(255,255,255,0.45); }
.binder-total {
  text-align: right;
  color: var(--text2);
  font-variant-numeric: tabular-nums;
  font-size: 13px;
}
.tooltip {
  position: fixed;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 6px 10px;
  pointer-events: none;
  font-size: 12px;
  display: none;
  z-index: 100;
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  max-width: 360px;
}
.tooltip .tk { color: var(--text2); margin-right: 6px; }
.tooltip .tline { white-space: nowrap; }
.tooltip .thint {
  color: var(--text2);
  font-size: 10px;
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px solid var(--border);
  white-space: nowrap;
}
.empty {
  text-align: center;
  color: var(--text2);
  padding: 80px 20px;
  font-size: 14px;
}
</style>
</head>
<body>
<header>
  <h1>MTG Collection Report</h1>
  <span class="subtitle" id="subtitle"></span>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="summary">
      <div class="stat"><div class="label">Total cards</div><div class="value" id="stat-total">0</div></div>
      <div class="stat"><div class="label">Distinct cards</div><div class="value" id="stat-distinct">0</div></div>
      <div class="stat"><div class="label">Binders</div><div class="value" id="stat-binders">0</div></div>
      <div class="stat"><div class="label">Sets</div><div class="value" id="stat-sets">0</div></div>
    </div>
    <div class="filter-group" data-key="binder"><div class="title"><h3>Binder</h3><div class="filter-actions"><span data-act="all">All</span><span data-act="none">None</span></div></div><div class="filter-list"></div></div>
    <div class="filter-group" data-key="set_code"><div class="title"><h3>Set</h3><div class="filter-actions"><span data-act="all">All</span><span data-act="none">None</span></div></div><div class="filter-list"></div></div>
    <div class="filter-group" data-key="rarity"><div class="title"><h3>Rarity</h3><div class="filter-actions"><span data-act="all">All</span><span data-act="none">None</span></div></div><div class="filter-list"></div></div>
    <div class="filter-group" data-key="color"><div class="title"><h3>Color</h3><div class="filter-actions"><span data-act="all">All</span><span data-act="none">None</span></div></div><div class="filter-list"></div></div>
  </aside>
  <main class="main">
    <div class="controls">
      <label>Inner breakdown:
        <select id="drill">
          <option value="color">Color</option>
          <option value="rarity">Rarity</option>
          <option value="set_code">Set</option>
        </select>
      </label>
    </div>
    <div class="legend" id="legend"></div>
    <div id="bars"></div>
  </main>
</div>
<div class="tooltip" id="tooltip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function() {
  const RAW = JSON.parse(document.getElementById('data').textContent);
  const records = RAW.records;
  const COLOR_PAL = RAW.color_palette;
  const RARITY_PAL = RAW.rarity_palette;
  const RARITY_ORDER = RAW.rarity_order;
  const COLOR_ORDER = RAW.color_order;
  const ALL_BINDERS = RAW.binders;
  const ALL_SETS = RAW.sets;
  const ALL_RARITIES = RAW.rarities_in_data;
  const ALL_COLORS = RAW.colors_in_data;
  const SET_NAMES = RAW.set_names;

  const state = {
    binder: new Set(ALL_BINDERS),
    set_code: new Set(ALL_SETS),
    rarity: new Set(ALL_RARITIES),
    color: new Set(ALL_COLORS),
    drill: 'color',
  };

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])
    );
  }

  function setCodeColor(code) {
    let h = 0;
    for (let i = 0; i < code.length; i++) {
      h = ((h * 31) + code.charCodeAt(i)) | 0;
    }
    const hue = ((h % 360) + 360) % 360;
    return 'hsl(' + hue + ', 45%, 52%)';
  }

  function colorFor(dim, value) {
    if (dim === 'color') return COLOR_PAL[value] || '#888';
    if (dim === 'rarity') return RARITY_PAL[value] || '#888';
    if (dim === 'set_code') return setCodeColor(value);
    return '#888';
  }

  const _lightBgCache = new Map();
  function isLightBg(cssColor) {
    if (_lightBgCache.has(cssColor)) return _lightBgCache.get(cssColor);
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;left:-9999px;top:-9999px;color:' + cssColor;
    document.body.appendChild(probe);
    const rgb = getComputedStyle(probe).color;
    document.body.removeChild(probe);
    const m = rgb.match(/\d+(\.\d+)?/g);
    let res = false;
    if (m && m.length >= 3) {
      const r = +m[0], g = +m[1], b = +m[2];
      // Perceived luminance (Rec. 709). Threshold tuned so MTG palette splits
      // cleanly: White / Multicolor / uncommon / rare → dark text;
      // Blue / Black / Red / Green / Land / Colorless → light text.
      res = (0.2126 * r + 0.7152 * g + 0.0722 * b) > 155;
    }
    _lightBgCache.set(cssColor, res);
    return res;
  }

  function valueOrder(dim, valueSet) {
    if (dim === 'color') return COLOR_ORDER.filter(v => valueSet.has(v));
    if (dim === 'rarity') return RARITY_ORDER.filter(v => valueSet.has(v));
    return [...valueSet].sort();
  }

  function makeSwatch(color, outline) {
    const sw = document.createElement('span');
    sw.className = 'swatch' + (outline ? ' outline' : '');
    sw.style.background = color;
    return sw;
  }

  function buildFilter(key, items, withSwatch) {
    const group = document.querySelector('.filter-group[data-key="' + key + '"]');
    const list = group.querySelector('.filter-list');
    list.innerHTML = '';
    for (const it of items) {
      const lab = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = it.value;
      cb.checked = state[key].has(it.value);
      cb.addEventListener('change', () => {
        if (cb.checked) state[key].add(it.value);
        else state[key].delete(it.value);
        render();
      });
      lab.appendChild(cb);
      if (withSwatch) {
        const sw = withSwatch(it.value);
        if (sw) lab.appendChild(sw);
      }
      const text = document.createElement('span');
      text.className = 'lbl';
      text.textContent = it.label;
      text.title = it.label;
      lab.appendChild(text);
      list.appendChild(lab);
    }
    group.querySelectorAll('.filter-actions span').forEach(span => {
      span.addEventListener('click', () => {
        const act = span.getAttribute('data-act');
        const boxes = list.querySelectorAll('input[type=checkbox]');
        state[key].clear();
        if (act === 'all') {
          boxes.forEach((cb, i) => { cb.checked = true; state[key].add(items[i].value); });
        } else {
          boxes.forEach(cb => { cb.checked = false; });
        }
        render();
      });
    });
  }

  function syncCheckboxes(key) {
    const group = document.querySelector('.filter-group[data-key="' + key + '"]');
    if (!group) return;
    const boxes = group.querySelectorAll('input[type=checkbox]');
    for (const cb of boxes) cb.checked = state[key].has(cb.value);
  }

  function buildAllFilters() {
    buildFilter('binder',
      ALL_BINDERS.map(b => ({ value: b, label: b }))
    );
    buildFilter('set_code',
      ALL_SETS.map(c => ({
        value: c,
        label: SET_NAMES[c] ? c + ' — ' + SET_NAMES[c] : c,
      }))
    );
    buildFilter('rarity',
      RARITY_ORDER.filter(r => ALL_RARITIES.includes(r))
        .map(r => ({ value: r, label: r }))
    );
    buildFilter('color',
      COLOR_ORDER.filter(c => ALL_COLORS.includes(c))
        .map(c => ({ value: c, label: c })),
      v => makeSwatch(COLOR_PAL[v], v === 'Black')
    );
  }

  function filtered() {
    return records.filter(r =>
      state.binder.has(r.binder) &&
      state.set_code.has(r.set_code) &&
      state.rarity.has(r.rarity) &&
      state.color.has(r.color)
    );
  }

  const tooltip = document.getElementById('tooltip');
  let lastTooltipHtml = '';
  function showTooltip(html, ev) {
    if (html !== null) {
      tooltip.innerHTML = html;
      lastTooltipHtml = html;
    }
    tooltip.style.display = 'block';
    let x = ev.clientX + 12;
    let y = ev.clientY + 12;
    const rect = tooltip.getBoundingClientRect();
    if (x + rect.width > window.innerWidth) x = ev.clientX - rect.width - 12;
    if (y + rect.height > window.innerHeight) y = ev.clientY - rect.height - 12;
    tooltip.style.left = x + 'px';
    tooltip.style.top = y + 'px';
  }
  function hideTooltip() { tooltip.style.display = 'none'; }

  function fitLabels() {
    const labels = document.querySelectorAll('.seg-label');
    for (const lbl of labels) {
      lbl.style.visibility = 'visible';
      // offsetWidth on an absolutely-positioned span reflects natural text width.
      if (lbl.offsetWidth + 2 > lbl.parentElement.clientWidth) {
        lbl.style.visibility = 'hidden';
      }
    }
  }

  function render() {
    const recs = filtered();

    const total = recs.reduce((s, r) => s + r.qty, 0);
    const ids = new Set();
    const bSet = new Set();
    const sSet = new Set();
    for (const r of recs) {
      ids.add(r.scryfall_id);
      bSet.add(r.binder);
      sSet.add(r.set_code);
    }
    document.getElementById('stat-total').textContent = total.toLocaleString();
    document.getElementById('stat-distinct').textContent = ids.size.toLocaleString();
    document.getElementById('stat-binders').textContent = bSet.size.toLocaleString();
    document.getElementById('stat-sets').textContent = sSet.size.toLocaleString();

    const drill = state.drill;
    const perBinder = new Map();
    for (const r of recs) {
      let bk = perBinder.get(r.binder);
      if (!bk) { bk = { total: 0, segs: new Map() }; perBinder.set(r.binder, bk); }
      bk.total += r.qty;
      const v = r[drill];
      let seg = bk.segs.get(v);
      if (!seg) { seg = { qty: 0, ids: new Set() }; bk.segs.set(v, seg); }
      seg.qty += r.qty;
      seg.ids.add(r.scryfall_id);
    }

    const binderRows = [...perBinder.entries()]
      .sort((a, b) => b[1].total - a[1].total);
    const maxTotal = binderRows.length ? binderRows[0][1].total : 0;

    const segValues = new Set();
    for (const [, bk] of binderRows) {
      for (const v of bk.segs.keys()) segValues.add(v);
    }
    const ordered = valueOrder(drill, segValues);

    const legend = document.getElementById('legend');
    legend.innerHTML = '';
    for (const v of ordered) {
      const item = document.createElement('div');
      item.className = 'item';
      item.appendChild(makeSwatch(colorFor(drill, v), drill === 'color' && v === 'Black'));
      const lbl = document.createElement('span');
      lbl.textContent = drill === 'set_code' && SET_NAMES[v] ? v + ' — ' + SET_NAMES[v] : v;
      item.appendChild(lbl);
      legend.appendChild(item);
    }

    const barsEl = document.getElementById('bars');
    barsEl.innerHTML = '';
    if (binderRows.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No cards match the current filters.';
      barsEl.appendChild(empty);
      return;
    }

    for (const [binder, bk] of binderRows) {
      const row = document.createElement('div');
      row.className = 'binder-row';

      const nameEl = document.createElement('div');
      nameEl.className = 'binder-name';
      nameEl.textContent = binder;
      nameEl.title = binder;
      row.appendChild(nameEl);

      const barWrap = document.createElement('div');
      barWrap.className = 'bar-wrap';
      const bar = document.createElement('div');
      bar.className = 'binder-bar';
      const widthPct = maxTotal ? (bk.total / maxTotal) * 100 : 0;
      bar.style.width = widthPct.toFixed(2) + '%';

      for (const v of ordered) {
        const seg = bk.segs.get(v);
        if (!seg) continue;
        const segEl = document.createElement('div');
        segEl.className = 'seg';
        const segBg = colorFor(drill, v);
        if (drill === 'color' && v === 'Black') segEl.classList.add('outline-black');
        segEl.style.background = segBg;
        segEl.style.width = ((seg.qty / bk.total) * 100).toFixed(3) + '%';

        const lblEl = document.createElement('span');
        lblEl.className = 'seg-label ' + (isLightBg(segBg) ? 'on-light' : 'on-dark');
        lblEl.textContent = seg.qty.toLocaleString();
        segEl.appendChild(lblEl);

        const labelDisplay = drill === 'set_code' && SET_NAMES[v]
          ? v + ' — ' + SET_NAMES[v] : v;
        const tipHtml =
          '<div class="tline"><span class="tk">binder:</span>' + escapeHtml(binder) + '</div>' +
          '<div class="tline"><span class="tk">' + escapeHtml(drill) + ':</span>' + escapeHtml(labelDisplay) + '</div>' +
          '<div class="tline"><span class="tk">qty:</span>' + seg.qty.toLocaleString() +
          ' &nbsp; <span class="tk">distinct:</span>' + seg.ids.size.toLocaleString() + '</div>' +
          '<div class="thint">click: focus filter · ctrl/⌘-click: exclude</div>';
        const segValue = v;
        segEl.addEventListener('mouseenter', ev => showTooltip(tipHtml, ev));
        segEl.addEventListener('mousemove', ev => showTooltip(null, ev));
        segEl.addEventListener('mouseleave', hideTooltip);
        segEl.addEventListener('click', ev => {
          const filterKey = state.drill;
          if (ev.ctrlKey || ev.metaKey) {
            state[filterKey].delete(segValue);
          } else {
            state[filterKey] = new Set([segValue]);
          }
          syncCheckboxes(filterKey);
          hideTooltip();
          render();
        });
        bar.appendChild(segEl);
      }
      barWrap.appendChild(bar);
      row.appendChild(barWrap);

      const totalEl = document.createElement('div');
      totalEl.className = 'binder-total';
      totalEl.textContent = bk.total.toLocaleString();
      row.appendChild(totalEl);

      barsEl.appendChild(row);
    }

    requestAnimationFrame(fitLabels);
  }

  buildAllFilters();
  document.getElementById('drill').addEventListener('change', e => {
    state.drill = e.target.value;
    render();
  });
  window.addEventListener('resize', fitLabels);
  document.getElementById('subtitle').textContent =
    records.length.toLocaleString() + ' entries · ' +
    ALL_BINDERS.length + ' binders · ' +
    ALL_SETS.length + ' sets · generated ' + RAW.generated_at;
  render();
})();
</script>
</body>
</html>
"""


def render_html(
    records: list[dict[str, Any]],
    set_names: dict[str, str],
    output_path: Path,
) -> None:
    binders = sorted({r["binder"] for r in records})
    sets_in_data = sorted({r["set_code"] for r in records})
    rarities_in_data = [r for r in RARITIES if any(rec["rarity"] == r for rec in records)]
    colors_in_data = [c for c in COLOR_ORDER if any(rec["color"] == c for rec in records)]

    payload_records = [
        {
            "binder": r["binder"],
            "set_code": r["set_code"],
            "rarity": r["rarity"],
            "color": r["color"],
            "scryfall_id": r["scryfall_id"],
            "qty": r["qty"],
        }
        for r in records
    ]

    payload = {
        "records": payload_records,
        "color_palette": COLOR_PALETTE,
        "rarity_palette": RARITY_PALETTE,
        "rarity_order": RARITIES,
        "color_order": COLOR_ORDER,
        "binders": binders,
        "sets": sets_in_data,
        "rarities_in_data": rarities_in_data,
        "colors_in_data": colors_in_data,
        "set_names": set_names,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    data_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    # Inside <script type="application/json">, "</" would close the tag.
    data_json_safe = data_json.replace("</", "<\\/")

    html = HTML_TEMPLATE.replace("__DATA__", data_json_safe)
    output_path.write_text(html, encoding="utf-8")
    logging.info("Wrote %s (%d bytes)", output_path, len(html))


# ---- Main -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an interactive HTML report from a ManaBox CSV export.",
    )
    parser.add_argument("--input", default=INPUT_CSV,
                        help=f"Input CSV (default: {INPUT_CSV})")
    parser.add_argument("--output", default=OUTPUT_HTML,
                        help=f"Output HTML (default: {OUTPUT_HTML})")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Force a fresh Scryfall bulk download.")
    parser.add_argument("--debug", action="store_true",
                        help="Dump intermediate report_data.json.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    input_path = Path(args.input)
    output_path = Path(args.output)
    cache_path = Path(SCRYFALL_CACHE_PATH)

    if not input_path.exists():
        logging.error("Input CSV not found: %s", input_path)
        return 2

    blacklist = set(BINDER_BLACKLIST)
    rows = load_csv(input_path, blacklist)
    if not rows:
        logging.error("No usable rows in CSV; aborting.")
        return 2

    needed = collect_card_ids(rows)
    logging.info("Need Scryfall data for %d unique IDs.", len(needed))

    enriched = enrich(needed, cache_path, refresh=args.refresh_cache)

    records, set_names = aggregate(rows, enriched)
    if not records:
        logging.error("No records after aggregation; aborting.")
        return 2

    if args.debug:
        debug_path = Path("report_data.json")
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump(
                {"records": records, "set_names": set_names},
                f, indent=2, ensure_ascii=False,
            )
        logging.info("Wrote %s", debug_path)

    render_html(records, set_names, output_path)
    logging.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
