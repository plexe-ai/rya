"""Document utilities for extraction agents - splitting and provenance merge.

Long documents break naive pipelines three ways: model per-call caps (bytes
AND pages), serial latency, and silent merge collisions. These helpers are the
platform half of the fix; the fan-out itself is ctx.jobs.schedule_group.
"""

from __future__ import annotations

from typing import Optional


def split_pdf(content: bytes, max_bytes: int = 3_000_000, max_pages: int = 25,
              overlap: int = 1) -> list:
    """Split a PDF into chunks under both byte and page caps.

    Returns ``[{bytes, pages: (start, end), part}]`` (1-indexed, inclusive).
    ``overlap`` repeats trailing pages at each boundary so tables spanning a
    page break are seen whole by some chunk. A single-chunk doc returns one
    entry. Requires pypdf; raises RyaError with an install hint if absent.
    """
    from .errors import RyaError
    try:
        import io
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise RyaError("E_VALIDATION", "PDF splitting requires pypdf.",
                       hint="pip install pypdf")
    try:
        reader = PdfReader(io.BytesIO(content))
        n = len(reader.pages)
    except Exception:
        # Malformed-but-model-readable PDFs exist in the wild (scanner output,
        # odd generators). Never kill the pipeline over parsing - fall back to
        # a single whole-document chunk and let the model try.
        return [{"bytes": content, "pages": (1, 1), "part": 0}]
    if n <= max_pages and len(content) <= max_bytes:
        return [{"bytes": content, "pages": (1, n), "part": 0}]
    # page-count step that also respects the byte cap (proportional estimate)
    per_page = max(1, len(content) // max(1, n))
    step = max(1, min(max_pages, max_bytes // per_page))
    chunks = []
    start = 0
    part = 0
    while start < n:
        end = min(n, start + step)
        w = PdfWriter()
        for pg in reader.pages[max(0, start - (overlap if part else 0)):end]:
            w.add_page(pg)
        buf = io.BytesIO()
        w.write(buf)
        chunks.append({"bytes": buf.getvalue(),
                       "pages": (max(1, start + 1 - (overlap if part else 0)), end),
                       "part": part})
        part += 1
        start = end
    return chunks


def merge_extractions(parts: list) -> dict:
    """Merge per-chunk extraction dicts with provenance and conflict detection.

    ``parts``: [{data: {field: value}, source: "p12-36"}]. Returns
    ``{fields: {field: {value, source}}, conflicts: [{field, values}]}``.
    A field seen with materially different non-null values in different parts
    becomes a CONFLICT - surfaced, never silently last-wins. Null values never
    overwrite real ones.
    """
    fields: dict = {}
    conflicts: dict = {}
    for part in parts:
        src = part.get("source", "?")
        for k, v in (part.get("data") or {}).items():
            if v is None:
                continue
            if k not in fields:
                fields[k] = {"value": v, "source": src}
                continue
            existing = fields[k]["value"]
            if _same(existing, v):
                continue
            conflicts.setdefault(k, [{"value": existing, "source": fields[k]["source"]}])
            conflicts[k].append({"value": v, "source": src})
    return {"fields": fields,
            "conflicts": [{"field": k, "values": v} for k, v in conflicts.items()]}


def _same(a, b) -> bool:
    if a == b:
        return True
    try:  # numeric near-equality (rounding across statements)
        fa, fb = float(a), float(b)
        return abs(fa - fb) <= max(1.0, abs(fa) * 0.005)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()
