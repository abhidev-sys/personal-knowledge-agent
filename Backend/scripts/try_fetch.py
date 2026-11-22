"""Manual check: fetch a real URL through the SSRF-safe fetcher and show what would be stored.

Run from backend/ with the venv active:
    python scripts/try_fetch.py https://example.com
    python scripts/try_fetch.py http://169.254.169.254/      (must be BLOCKED)
"""
import sys

from app.core.exceptions import AppError
from app.ingestion.chunking import assemble_document, chunk_document
from app.ingestion.fetcher import SafeFetcher
from app.ingestion.parsers import parse_html, parse_plain_text


def main(url: str) -> int:
    try:
        page = SafeFetcher().fetch(url)
        if page.content_type == "text/plain":
            document = parse_plain_text(page.content.decode("utf-8", errors="replace"))
        else:
            document = parse_html(page.content, page_url=page.final_url)
    except AppError as exc:
        print(f"REFUSED/FAILED [{exc.code}]: {exc.message}")
        return 1
    text, placed = assemble_document(document.sections)
    chunks = chunk_document(placed, size=1000, overlap=150)
    print("final url :", page.final_url)
    print("type/bytes:", page.content_type, len(page.content))
    print("title     :", document.title)
    print("canonical :", document.canonical_url)
    print("sections  :", len(document.sections), "| chars:", len(text), "| chunks:", len(chunks))
    if chunks:
        print("first chunk:", chunks[0].content[:200].replace("\n", " "))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/try_fetch.py <url>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))