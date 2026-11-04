"""Element fingerprints: capture from a live page and score similarity.

A fingerprint is a plain dict so it serializes straight into memory and reports:
  tag, id, classes, role, name (accessible name), text, attrs, dom_path, bbox, context
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from playwright.sync_api import Locator

_CAPTURE_JS = r"""
(el) => {
  const implicitRole = (e) => {
    const t = e.tagName.toLowerCase();
    if (e.getAttribute('role')) return e.getAttribute('role');
    if (t === 'button' || (t === 'input' && ['submit','button'].includes(e.type))) return 'button';
    if (t === 'a' && e.hasAttribute('href')) return 'link';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'input') return 'textbox';
    return 'generic';
  };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const domPath = (e) => {
    const parts = [];
    while (e && e.nodeType === 1 && e.tagName.toLowerCase() !== 'html') {
      let i = 1, s = e;
      while ((s = s.previousElementSibling)) if (s.tagName === e.tagName) i++;
      parts.unshift(e.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      e = e.parentElement;
    }
    return parts.join(' > ');
  };
  const attrs = {};
  for (const a of el.attributes) {
    if (['id','class','style'].includes(a.name)) continue;
    attrs[a.name] = a.value.slice(0, 120);
  }
  const r = el.getBoundingClientRect();
  const text = norm(el.innerText);
  // Context: the nearest ancestor (up to 4 levels) whose text says more than the element's own,
  // e.g. the product card around an "Add to cart" button.
  let parent = el.parentElement;
  for (let i = 0; parent && i < 4 && norm(parent.innerText).length <= text.length + 10; i++) parent = parent.parentElement;
  return {
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    classes: Array.from(el.classList),
    role: implicitRole(el),
    name: norm(el.getAttribute('aria-label') || text || el.value || el.title).slice(0, 120),
    text: text.slice(0, 200),
    attrs,
    dom_path: domPath(el),
    bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    context: norm(parent ? parent.innerText : '').slice(0, 160),
  };
}
"""


def capture(el: Locator) -> dict[str, Any]:
    return el.evaluate(_CAPTURE_JS)


def _ratio(a: str | None, b: str | None) -> float:
    a, b = (a or "").lower(), (b or "").lower()
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _tokens(fp: dict[str, Any]) -> set[str]:
    raw = " ".join(
        [fp.get("id") or "", " ".join(fp.get("classes") or [])]
        + [str(v) for k, v in (fp.get("attrs") or {}).items() if k in ("data-testid", "name", "value", "type", "href")]
    )
    return {t for t in re.split(r"[^a-z0-9]+", raw.lower()) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _bbox_proximity(a: dict | None, b: dict | None) -> float:
    if not a or not b:
        return 0.0
    dist = ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5
    size = 1.0 - min(1.0, abs(a["w"] * a["h"] - b["w"] * b["h"]) / max(1, a["w"] * a["h"], b["w"] * b["h"]))
    return 0.6 * max(0.0, 1.0 - dist / 600.0) + 0.4 * size


WEIGHTS = {"role": 0.25, "tag": 0.10, "name": 0.30, "tokens": 0.15, "context": 0.10, "bbox": 0.10}


def similarity(recorded: dict[str, Any], live: dict[str, Any]) -> dict[str, float]:
    """Return per-feature scores plus a weighted `total` in [0, 1]."""
    parts = {
        "role": 1.0 if recorded.get("role") == live.get("role") else 0.0,
        "tag": 1.0 if recorded.get("tag") == live.get("tag") else 0.0,
        "name": _ratio(recorded.get("name"), live.get("name")),
        "tokens": _jaccard(_tokens(recorded), _tokens(live)),
        "context": _ratio(recorded.get("context"), live.get("context")),
        "bbox": _bbox_proximity(recorded.get("bbox"), live.get("bbox")),
    }
    parts["total"] = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 3)
    return {k: round(v, 3) for k, v in parts.items()}
