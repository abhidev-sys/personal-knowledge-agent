"""Turn raw inputs (text, Markdown, PDF bytes, HTML) into ParsedDocument objects.

Nothing here executes imported content: HTML is only parsed as a tree, never rendered or run.
"""
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from app.core.exceptions import IngestionError
from app.ingestion.types import ParsedDocument, Section

MIN_ARTICLE_CHARS = 40

# ------------------------------------------------------------------------- plain text
def parse_plain_text(text: str, title: str | None = None) -> ParsedDocument:
    return ParsedDocument(sections=[Section(text=text)], title=title)


# --------------------------------------------------------------------------- markdown
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _strip_front_matter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1 or end > 4000:
        return text
    after = text.find("\n", end + 4)
    return text[after + 1 :] if after != -1 else ""


def parse_markdown(text: str) -> ParsedDocument:
    """Split on ATX headings (# ...). Headings inside code fences are ignored."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_front_matter(text)
    sections: list[Section] = []
    buffer: list[str] = []
    stack: list[tuple[int, str]] = []
    current_heading: str | None = None
    title: str | None = None
    fence: str | None = None

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(Section(text=body, page_number=None, heading=current_heading))
        buffer.clear()

    for line in text.split("\n"):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            buffer.append(line)
            continue
        heading = None if fence else _HEADING.match(line)
        if heading:
            flush()
            level, name = len(heading.group(1)), heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
            current_heading = " > ".join(n for _, n in stack)[:500]
            if title is None and level == 1:
                title = name
        buffer.append(line)
    flush()
    return ParsedDocument(sections=sections, title=title)


# ------------------------------------------------------------------------------- PDF
def parse_pdf(data: bytes, *, max_pages: int) -> ParsedDocument:
    """Extract text page by page. Scanned/image-only PDFs are reported, never faked."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise IngestionError("Could not open the PDF (it may be corrupt or not a PDF).") from exc
    try:
        if doc.needs_pass:
            raise IngestionError("The PDF is password-protected.")
        if doc.page_count > max_pages:
            raise IngestionError(
                f"The PDF has {doc.page_count} pages; the limit is {max_pages}."
            )
        sections: list[Section] = []
        empty_pages = 0
        for index in range(doc.page_count):
            text = doc.load_page(index).get_text("text")
            if text.strip():
                sections.append(Section(text=text, page_number=index + 1))
            else:
                empty_pages += 1
        if not sections:
            raise IngestionError(
                "No extractable text found. This looks like a scanned or image-only PDF. "
                "OCR is not implemented yet, so it cannot be indexed."
            )
        warnings = []
        if empty_pages:
            warnings.append(f"{empty_pages} page(s) had no extractable text (images/scans).")
        title = (doc.metadata or {}).get("title") or None
        return ParsedDocument(
            sections=sections, title=title, page_count=doc.page_count, warnings=warnings
        )
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError("Could not read text from the PDF.") from exc
    finally:
        doc.close()


# ------------------------------------------------------------------------------ HTML
_NOISE_TAGS = [
    "script", "style", "noscript", "template", "svg", "iframe", "form", "nav", "footer",
    "aside", "button", "select", "canvas", "dialog",
]
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_INLINE = {
    "a", "span", "b", "strong", "i", "em", "u", "code", "small", "sup", "sub", "mark", "abbr",
    "cite", "time", "q", "s", "del", "ins", "label", "font",
}
_LEAF_BLOCKS = {"p", "li", "blockquote", "td", "th", "dd", "dt", "figcaption", "caption", "summary"}
_STRUCTURAL = sorted(_LEAF_BLOCKS | _HEADINGS | {"pre"})


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_html(html: str | bytes, *, page_url: str | None = None) -> ParsedDocument:
    """Extract the main readable text of an article page."""
    soup = BeautifulSoup(html, "html.parser")
    title = _html_title(soup)
    canonical = _html_canonical(soup, page_url)

    # Site-specific clutter (menus, edit links, reference markers, infoboxes).
    for tag in soup.select(
        ".navbox, .vector-menu, .mw-editsection, .reference, .noprint, .toc, #toc, .infobox"
    ):
        tag.decompose()
    for tag in soup.find_all(_NOISE_TAGS):
        if not getattr(tag, "decomposed", False):
            tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(" ")

    articles = soup.find_all("article")
    if articles:
        root: Tag = max(articles, key=lambda a: len(a.get_text()))
    else:
        root = (
            soup.find(id="mw-content-text")  # MediaWiki / Wikipedia article body
            or soup.find("main")
            or soup.find(attrs={"role": "main"})
            or soup.body
            or soup
        )

    items: list[tuple] = []
    _walk(root, items)
    sections = _build_sections(items)
    if sum(len(s.text) for s in sections) < MIN_ARTICLE_CHARS:
        raise IngestionError(
            "Could not find readable text on this page (it may need JavaScript or a login)."
        )
    return ParsedDocument(sections=sections, title=title, canonical_url=canonical)


def _html_title(soup: BeautifulSoup) -> str | None:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _collapse(str(og["content"])) or None
    if soup.title:
        text = _collapse(soup.title.get_text())
        if text:
            return text
    h1 = soup.find("h1")
    return (_collapse(h1.get_text(" ")) or None) if h1 else None


def _html_canonical(soup: BeautifulSoup, page_url: str | None) -> str | None:
    link = soup.find("link", rel="canonical")
    href = link.get("href") if link else None
    if not href:
        return None
    absolute = urljoin(page_url, str(href)) if page_url else str(href)
    try:
        return absolute if urlsplit(absolute).scheme in ("http", "https") else None
    except ValueError:
        return None


def _walk(node: Tag, out: list[tuple]) -> None:
    """Emit ("h", level, text) for headings and ("t", text) for text blocks, in order."""
    run: list[str] = []

    def flush() -> None:
        text = _collapse("".join(run))
        if text:
            out.append(("t", text))
        run.clear()

    for child in node.children:
        if type(child) is NavigableString:  # plain text, not comments/doctype
            run.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in _INLINE:
            run.append(child.get_text(""))
            continue
        flush()
        if name in _HEADINGS:
            out.append(("h", int(name[1]), _collapse(child.get_text(""))))
        elif name == "pre":
            code = child.get_text().strip("\n")
            if code.strip():
                out.append(("t", code))
        elif name in _LEAF_BLOCKS and child.find(_STRUCTURAL) is None:
            text = _collapse(child.get_text(""))
            if text:
                out.append(("t", text))
        else:
            _walk(child, out)
    flush()


def _build_sections(items: list[tuple]) -> list[Section]:
    sections: list[Section] = []
    buffer: list[str] = []
    stack: list[tuple[int, str]] = []
    heading: str | None = None

    def flush() -> None:
        text = "\n\n".join(buffer).strip()
        if text:
            sections.append(Section(text=text, heading=heading))
        buffer.clear()

    for item in items:
        if item[0] == "h":
            _, level, name = item
            if not name:
                continue
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
            heading = " > ".join(n for _, n in stack)[:500]
            buffer.append(name)
        else:
            buffer.append(item[1])
    flush()
    return sections