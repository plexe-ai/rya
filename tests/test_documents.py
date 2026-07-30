"""Document utilities - splitting caps + provenance merge."""
import io

import pytest

from rya.documents import merge_extractions, split_pdf


def _pdf(pages: int) -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO(); w.write(buf)
    return buf.getvalue()


def test_split_respects_page_cap_with_overlap():
    chunks = split_pdf(_pdf(60), max_pages=25, overlap=1)
    assert len(chunks) == 3
    assert chunks[0]["pages"] == (1, 25)
    assert chunks[1]["pages"][0] == 25          # overlap page repeated
    assert chunks[-1]["pages"][1] == 60
    assert split_pdf(_pdf(10), max_pages=25) [0]["part"] == 0  # small: single chunk


def test_merge_provenance_and_conflicts():
    out = merge_extractions([
        {"data": {"revenue": 14200000, "score": None}, "source": "p1-25"},
        {"data": {"revenue": 14200001, "assets": 9800000}, "source": "p25-50"},  # ~equal
        {"data": {"revenue": 9999999}, "source": "p50-60"},                      # conflict
    ])
    assert out["fields"]["revenue"]["value"] == 14200000
    assert out["fields"]["assets"]["source"] == "p25-50"
    assert "score" not in out["fields"]                     # nulls never land
    assert len(out["conflicts"]) == 1
    c = out["conflicts"][0]
    assert c["field"] == "revenue" and len(c["values"]) == 2  # flagged, not last-wins
