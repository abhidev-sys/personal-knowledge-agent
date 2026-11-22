import pymupdf
import pytest

from app.core.exceptions import IngestionError
from app.ingestion.parsers import parse_html, parse_markdown, parse_pdf, parse_plain_text


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


# ------------------------------------------------------------------------- plain / markdown
def test_plain_text_is_one_section():
    doc = parse_plain_text("hello", title="T")
    assert doc.title == "T" and len(doc.sections) == 1


def test_markdown_headings_title_and_code_fences():
    md = (
        "# Guide\nintro text\n\n## Setup\nrun it\n```\n# not a heading\n```\n## Use\nenjoy"
    )
    doc = parse_markdown(md)
    assert doc.title == "Guide"
    assert [s.heading for s in doc.sections] == ["Guide", "Guide > Setup", "Guide > Use"]
    assert "# not a heading" in doc.sections[1].text


def test_markdown_without_headings_is_one_section():
    doc = parse_markdown("just some text\nmore text")
    assert doc.title is None
    assert len(doc.sections) == 1 and doc.sections[0].heading is None


def test_markdown_heading_with_hash_inside_and_closing_hashes():
    doc = parse_markdown("# C# basics ##\ntext")
    assert doc.title == "C# basics"


def test_markdown_front_matter_is_dropped():
    doc = parse_markdown("---\ntags: x\n---\n# Hello\nbody")
    assert doc.title == "Hello"
    assert "tags" not in doc.sections[0].text


# --------------------------------------------------------------------------------- PDF
def test_pdf_pages_keep_numbers():
    doc = parse_pdf(make_pdf(["Retrieval augmented generation", "Vector databases"]), max_pages=10)
    assert doc.page_count == 2
    assert [s.page_number for s in doc.sections] == [1, 2]
    assert "Retrieval augmented generation" in doc.sections[0].text
    assert "Vector databases" in doc.sections[1].text


def test_pdf_with_some_blank_pages_warns():
    doc = parse_pdf(make_pdf(["Hello world text", ""]), max_pages=10)
    assert doc.page_count == 2 and len(doc.sections) == 1
    assert any("1 page" in w for w in doc.warnings)


def test_image_only_pdf_is_reported_not_faked():
    with pytest.raises(IngestionError, match="OCR"):
        parse_pdf(make_pdf(["", ""]), max_pages=10)


def test_pdf_page_limit():
    with pytest.raises(IngestionError, match="pages"):
        parse_pdf(make_pdf(["a", "b", "c"]), max_pages=2)


def test_garbage_is_not_a_pdf():
    with pytest.raises(IngestionError):
        parse_pdf(b"this is not a pdf", max_pages=10)


# -------------------------------------------------------------------------------- HTML
ARTICLE = """
<html><head><title>My Article</title>
<link rel="canonical" href="/a/b">
<meta property="og:title" content="OG Title"></head>
<body><nav>Menu Home About</nav>
<article><h1>Heading One</h1>
<p>First paragraph with <b>bold</b>, and text.</p>
<h2>Part Two</h2><p>Second paragraph here.</p></article>
<footer>Copyright</footer><script>alert(1)</script></body></html>
"""


def test_html_extracts_article_title_canonical_and_headings():
    doc = parse_html(ARTICLE, page_url="https://site.test/x")
    full = "\n".join(s.text for s in doc.sections)
    assert doc.title == "OG Title"
    assert doc.canonical_url == "https://site.test/a/b"
    assert "First paragraph with bold, and text." in full
    assert "Second paragraph here." in full
    for junk in ("Menu", "Copyright", "alert"):
        assert junk not in full
    assert [s.heading for s in doc.sections] == ["Heading One", "Heading One > Part Two"]


def test_html_title_falls_back_to_title_tag():
    html = "<html><head><title>Plain Title</title></head><body><p>" + "word " * 20 + "</p></body></html>"
    assert parse_html(html).title == "Plain Title"


def test_html_without_article_uses_body():
    html = "<body><div><p>" + "Readable body text. " * 5 + "</p></div></body>"
    assert "Readable body text." in parse_html(html).sections[0].text


def test_html_nested_lists_keep_both_levels():
    html = (
        "<body><ul><li>Item one <ul><li>Sub item</li></ul></li></ul>"
        "<p>some more filler text to pass the minimum length threshold</p></body>"
    )
    text = "\n".join(s.text for s in parse_html(html).sections)
    assert "Item one" in text and "Sub item" in text


def test_html_pre_keeps_line_breaks():
    html = (
        "<body><pre>line one\nline two\nline three is here and it is long enough</pre></body>"
    )
    assert "line one\nline two" in parse_html(html).sections[0].text

def test_html_with_too_little_text_is_rejected():
    with pytest.raises(IngestionError, match="readable text"):
        parse_html("<html><body><script>app()</script><p>Hi</p></body></html>")


def test_html_javascript_canonical_is_ignored():
    html = (
        '<head><link rel="canonical" href="javascript:alert(1)"></head>'
        "<body><p>" + "text " * 20 + "</p></body>"
    )
    assert parse_html(html, page_url="https://a.test/").canonical_url is None