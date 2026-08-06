"""Shared test fixtures: a small corpus with one file of every supported type.

The PDF is assembled byte-by-byte (with a correct xref table) so the test
suite has zero dependency on any PDF-writing library.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest


def make_minimal_pdf(text: str) -> bytes:
    """Build a tiny valid single-page PDF containing `text`."""
    header = b"%PDF-1.4\n"
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    body = b""
    offsets = []
    pos = len(header)
    for i, obj in enumerate(objects, start=1):
        offsets.append(pos)
        piece = f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        body += piece
        pos += len(piece)
    xref_pos = len(header) + len(body)
    xref = b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    xref += b"0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()
    trailer = (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
        b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    )
    return header + body + xref + trailer


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A corpus folder with md, txt, csv, tsv, pdf, docx, and xlsx files."""
    root = tmp_path / "corpus"
    root.mkdir()

    (root / "handbook.md").write_text(
        "# Operations Handbook\n\n"
        "## Backup Policy\n\nBackups run nightly at 0200 and are kept for 30 days.\n\n"
        "## Escalation\n\nSeverity-one incidents page the on-call engineer immediately.\n",
        encoding="utf-8",
    )
    (root / "notes.txt").write_text(
        "The quarterly maintenance window is the second Saturday of the month.",
        encoding="utf-8",
    )

    with (root / "inventory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["asset", "owner", "status"])
        w.writerow(["firewall-01", "network team", "active"])
        w.writerow(["switch-12", "network team", "decommissioned"])

    (root / "codes.tsv").write_text(
        "code\tmeaning\nE42\tdisk failure imminent\nE7\tfan degraded\n",
        encoding="utf-8",
    )

    (root / "briefing.pdf").write_bytes(
        make_minimal_pdf("Uplink briefing: the migration freeze ends March twelfth")
    )

    docx = pytest.importorskip("docx", reason="python-docx not installed")

    d = docx.Document()
    d.add_heading("Access Control Procedure", level=1)
    d.add_paragraph("Badge requests require manager approval within two business days.")
    d.add_heading("Revocation", level=2)
    d.add_paragraph("Departing employees lose badge access on their final day.")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "zone"
    t.rows[0].cells[1].text = "clearance"
    t.rows[1].cells[0].text = "server room"
    t.rows[1].cells[1].text = "level 3"
    d.save(str(root / "procedure.docx"))

    openpyxl = pytest.importorskip("openpyxl", reason="openpyxl not installed")
    Workbook = openpyxl.Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["line item", "amount"])
    ws.append(["tailscale subscription", 0])
    ws.append(["backup drive", 129.99])
    wb.save(str(root / "budget.xlsx"))

    # Files that must be ignored.
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n0000")
    hidden = root / ".git"
    hidden.mkdir()
    (hidden / "config.md").write_text("should never be indexed", encoding="utf-8")

    return root
