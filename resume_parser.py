"""Extract plain text from uploaded resumes (PDF / DOCX).

Unlike the reference repo (which rasterised only page 1 with poppler on a hard-coded
Windows path), this reads every page as text, needs no system binaries, and works the
same on Windows, Linux and in Docker.
"""
import re

import docx
from pypdf import PdfReader


def extract_text(path: str) -> str:
    lower = path.lower()
    try:
        if lower.endswith(".pdf"):
            reader = PdfReader(path)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif lower.endswith(".docx"):
            d = docx.Document(path)
            parts = [p.text for p in d.paragraphs]
            for table in d.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            text = "\n".join(parts)
        else:
            return ""
    except Exception:  # corrupt / encrypted files
        return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
