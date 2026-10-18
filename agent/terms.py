"""Clinic terms: this clinic's own words and how the ASR tends to mishear them.

data/clinic_terms.json (CLINIC_TERMS_FILE to change) holds a small list:
  {"term": "Bisolvon", "kind": "drug|shorthand|panel|other", "meaning": "Bromhexine cough syrup",
   "maps_to": "Bromhexine", "mishearings": ["by solvent"]}

Used in three places:
  1. correct_segments: known mishearings in the transcript are replaced with the term, right after ASR
     (the ASR model takes no vocabulary hints, so this is where the list fixes it).
  2. glossary: the list goes into Lightning's prompt so it reads the shorthand correctly.
  3. Workspace.check_drug resolves a drug term to its formulary entry via maps_to; a drug name that
     matches neither the formulary nor this list is flagged rather than guessed.
"""

import json
import os
import re
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "clinic_terms.json"


def terms_file():
    return Path(os.environ.get("CLINIC_TERMS_FILE") or DEFAULT_FILE)


KINDS = ("drug", "shorthand", "panel", "other")
MAX_TERMS = 500


def load_terms(path=None):
    try:
        raw = json.loads(Path(path or terms_file()).read_text()).get("terms", [])
        return clean_terms(raw, strict=False)
    except (OSError, ValueError, AttributeError):
        return []


def clean_terms(raw, strict=True):
    """Validate an edited list; raises ValueError with a message the UI can show.
    strict=False (loading a hand-edited file) skips bad rows instead."""
    if not isinstance(raw, list):
        raise ValueError("terms must be a list")
    if strict and len(raw) > MAX_TERMS:
        raise ValueError(f"at most {MAX_TERMS} terms")
    out, seen = [], set()
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            if not strict:
                continue
            raise ValueError(f"row {i}: not an object")
        term = str(t.get("term", "")).strip()
        if not term:
            continue  # blank rows from the editor are dropped
        if term.lower() in seen:
            if not strict:
                continue
            raise ValueError(f"row {i}: '{term}' is listed twice")
        seen.add(term.lower())
        kind = str(t.get("kind") or "other").strip().lower()
        mishearings = t.get("mishearings") or []
        if isinstance(mishearings, str):
            mishearings = mishearings.split(",")
        out.append({
            "term": term,
            "kind": kind if kind in KINDS else "other",
            "meaning": str(t.get("meaning", "")).strip(),
            "maps_to": str(t.get("maps_to", "")).strip(),
            "mishearings": [m for m in (str(m).strip() for m in mishearings) if m and m.lower() != term.lower()],
        })
    return out


def save_terms(raw, path=None):
    terms = clean_terms(raw)
    path = Path(path or terms_file())
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data["terms"] = terms
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return terms


def _pattern(phrase):
    # Whole words only, any spacing, any case: "by  Solvent" matches "by solvent".
    words = [re.escape(w) for w in phrase.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


def correct_text(text, terms):
    """Return (text, [{"heard", "term"}]) with every known mishearing replaced by its term."""
    changes = []
    # Longest phrase first, so "panadol active fast" wins over a shorter overlapping one.
    pairs = sorted(((m, t["term"]) for t in terms for m in t.get("mishearings", [])), key=lambda p: -len(p[0]))
    for heard, term in pairs:
        def swap(m, term=term):
            changes.append({"heard": m.group(0), "term": term})
            return term
        text = _pattern(heard).sub(swap, text)
    return text, changes


def correct_segments(segments, terms):
    """Correct segment texts in place; returns the list of changes with the turn index."""
    log = []
    for i, s in enumerate(segments):
        s["text"], changes = correct_text(s["text"], terms)
        log += [{"turn": i, **c} for c in changes]
    return log


def find_term(name, terms):
    key = " ".join(name.lower().split())
    for t in terms:
        names = [t["term"].lower()] + [m.lower() for m in t.get("mishearings", [])]
        if key in names or any(len(n) >= 5 and _pattern(n).search(key) for n in names):
            return t
    return None


def glossary(terms):
    if not terms:
        return ""
    lines = []
    for t in terms:
        target = f" = {t['maps_to']}" if t.get("maps_to") and t["maps_to"].lower() not in t.get("meaning", "").lower() else ""
        lines.append(f"- {t['term']} ({t['kind']}): {t.get('meaning') or 'clinic term'}{target}")
    return "This clinic's own terms (brand names, shorthand, panels):\n" + "\n".join(lines)
