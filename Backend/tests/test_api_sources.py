import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.limits import LengthRequiredError, check_body_headers
from app.core.exceptions import IngestionError, InvalidInputError, PayloadTooLargeError
from app.db.base import Base
from app.db.models import DocumentChunk, User, UserSettings
from app.jobs.handlers import UrlIngestHandler
from app.jobs.queue import JOB_INGEST_URL
from app.jobs.worker import JobWorker
from app.main import create_app
from app.services.ingestion_service import IngestionService

API = "/api/v1/sources"
RAG_TEXT = "Retrieval augmented generation combines search with language models. " * 3
ARTICLE = (
    "<html><head><title>My Article</title></head><body><article><h1>Heading</h1><p>"
    + RAG_TEXT
    + "</p></article></body></html>"
)


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def error_code(response) -> str:
    return response.json()["error"]["code"]


def note(client, title="A note", content=RAG_TEXT, **extra):
    response = client.post(f"{API}/notes", json={"title": title, "content": content, **extra})
    assert response.status_code == 201, response.text
    return response.json()["source"]["id"]


def upload(client, name="a.md", data=b"# A\nsome markdown text here", mime="text/markdown", **form):
    return client.post(f"{API}/upload", files={"file": (name, data, mime)}, data=form)


def run_worker(client, fetcher) -> None:
    app = client.app

    def build(session):
        return IngestionService(
            session,
            app.state.settings,
            storage=app.state.storage,
            embedder=app.state.embedder,
            fetcher=fetcher,
        )

    worker = JobWorker(
        app.state.session_factory,
        {JOB_INGEST_URL: UrlIngestHandler(build)},
        worker_id="api-test",
        retry_base_seconds=0.0,
    )
    for _ in range(20):
        if not worker.run_once():
            return
    raise AssertionError("the worker never ran out of jobs")


@pytest.fixture
def make_client(settings):
    clients = []

    def build(**overrides):
        app = create_app(settings.model_copy(update=overrides))
        test_client = TestClient(app, raise_server_exceptions=False)
        test_client.__enter__()
        Base.metadata.create_all(app.state.engine)
        clients.append(test_client)
        return test_client

    yield build
    for test_client in clients:
        test_client.__exit__(None, None, None)


# ===================================================================== smoke / auth
def test_openapi_lists_the_source_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in (API, f"{API}/notes", f"{API}/upload", f"{API}/bookmarks", f"{API}/{{source_id}}/file"):
        assert path in paths


def test_requests_need_authentication_when_dev_auth_is_off(make_client):
    anonymous = make_client(dev_auth_enabled=False)
    response = anonymous.get(API)
    assert response.status_code == 401
    assert error_code(response) == "authentication_required"
    assert anonymous.get("/health").status_code == 200  # health stays open


def test_dev_user_is_created_once_with_default_settings(client):
    for _ in range(3):
        client.get(API)
    with client.app.state.session_factory() as session:
        users = session.scalars(select(User)).all()
        assert [u.email for u in users] == ["local@example.com"]
        assert session.get(UserSettings, users[0].id) is not None


# ============================================================================= notes
def test_create_note_then_the_same_note_again(client):
    payload = {"title": "RAG", "content": RAG_TEXT, "tags": ["RAG", "ai"]}
    first = client.post(f"{API}/notes", json=payload)
    assert first.status_code == 201
    body = first.json()
    assert body["created"] is True
    assert body["source"]["status"] == "completed"
    assert body["source"]["tags"] == ["ai", "rag"]
    assert body["source"]["chunk_count"] >= 1
    again = client.post(f"{API}/notes", json=payload)
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["source"]["id"] == body["source"]["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "   "},
        {"content": ""},
        {"title": "no content"},
        {"content": "ok", "bogus": 1},
        {"content": "ok", "tags": ["x" * 65]},
        {"content": "ok", "tags": [f"t{i}" for i in range(21)]},
    ],
)
def test_bad_note_payloads_are_422(client, payload):
    assert client.post(f"{API}/notes", json=payload).status_code == 422


def test_detail_content_and_chunks(client):
    source_id = note(client, "Detail")
    detail = client.get(f"{API}/{source_id}").json()
    assert detail["content_preview"].startswith("Retrieval augmented")
    assert detail["has_file"] is False
    assert client.get(f"{API}/{source_id}/content").json()["content"] == RAG_TEXT.strip()
    chunks = client.get(f"{API}/{source_id}/chunks").json()
    assert len(chunks) == detail["chunk_count"]
    assert chunks[0]["sequence"] == 0
    assert chunks[0]["has_embedding"] is True
    assert chunks[0]["embedding_model"] == "test-hashing-768"
    assert "embedding" not in chunks[0]


def test_list_filters_and_pagination(client):
    for index in range(3):
        note(client, f"Note {index}", f"unique content number {index}", tags=["even" if index % 2 == 0 else "odd"])
    assert upload(client).status_code == 201
    assert client.get(API).json()["total"] == 4
    assert client.get(API, params={"type": "note"}).json()["total"] == 3
    assert client.get(API, params={"tag": "EVEN"}).json()["total"] == 2
    assert client.get(API, params={"status": "completed"}).json()["total"] == 4
    page = client.get(API, params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    assert page["limit"] == 2 and page["offset"] == 2


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"type": "video"}, {"status": "weird"}],
)
def test_bad_list_parameters_are_422(client, params):
    assert client.get(API, params=params).status_code == 422


def test_invalid_source_id_is_422(client):
    response = client.get(f"{API}/not-a-uuid")
    assert response.status_code == 422
    assert error_code(response) == "validation_error"


# ========================================================================= editing
def test_patch_note_content_and_tags(client):
    source_id = note(client, "N", "alpha beta gamma")
    old_ids = {c["id"] for c in client.get(f"{API}/{source_id}/chunks").json()}
    response = client.patch(f"{API}/{source_id}", json={"content": "delta epsilon zeta", "tags": ["x"]})
    assert response.status_code == 200
    assert response.json()["tags"] == ["x"]
    new_chunks = client.get(f"{API}/{source_id}/chunks").json()
    assert {c["id"] for c in new_chunks}.isdisjoint(old_ids)
    assert all("alpha" not in c["content"] for c in new_chunks)
    assert client.get(f"{API}/{source_id}/content").json()["content"] == "delta epsilon zeta"
    assert client.patch(f"{API}/{source_id}", json={"tags": []}).json()["tags"] == []


@pytest.mark.parametrize("payload", [{}, {"bogus": 1}])
def test_patch_needs_a_known_field(client, payload):
    source_id = note(client)
    assert client.patch(f"{API}/{source_id}", json=payload).status_code == 422


def test_only_notes_can_have_their_content_edited(client):
    source_id = upload(client).json()["source"]["id"]
    response = client.patch(f"{API}/{source_id}", json={"content": "changed"})
    assert response.status_code == 422 and error_code(response) == "invalid_input"
    assert client.patch(f"{API}/{source_id}", json={"tags": ["ok"]}).status_code == 200


# ========================================================================== uploads
def test_markdown_upload_and_original_download(client):
    data = b"# Guide\nintro text here\n\n## Setup\nrun the installer\n"
    response = upload(client, "guide.md", data, tags=["Docs", "setup"])
    assert response.status_code == 201
    source = response.json()["source"]
    assert source["source_type"] == "markdown" and source["title"] == "Guide"
    assert source["tags"] == ["docs", "setup"]
    assert source["original_filename"] == "guide.md"
    download = client.get(f"{API}/{source['id']}/file")
    assert download.status_code == 200
    assert download.content == data
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-type"].startswith("text/markdown")
    assert upload(client, "guide.md", data).status_code == 200  # identical upload: no second copy


def test_pdf_upload_keeps_page_numbers(client):
    pdf = make_pdf(["Retrieval augmented generation", "Vector databases"])
    response = upload(client, "rag.pdf", pdf, "application/pdf")
    assert response.status_code == 201
    source = response.json()["source"]
    assert source["page_count"] == 2
    chunks = client.get(f"{API}/{source['id']}/chunks").json()
    assert {c["page_number"] for c in chunks} == {1, 2}
    assert client.get(f"{API}/{source['id']}/file").headers["content-type"].startswith("application/pdf")


def test_image_only_pdf_is_reported_not_faked(client):
    response = upload(client, "scan.pdf", make_pdf(["", ""]), "application/pdf")
    assert response.status_code == 422
    assert error_code(response) == "ingestion_failed"
    assert "OCR" in response.json()["error"]["message"]
    assert client.get(API).json()["total"] == 0


@pytest.mark.parametrize(
    "name,data",
    [("tool.exe", b"MZ"), ("empty.md", b""), ("noext", b"hello"), ("fake.pdf", b"just text"), ("bin.txt", b"\x80\x81\xff")],
)
def test_bad_uploads_are_422(client, name, data):
    assert upload(client, name, data, "application/octet-stream").status_code == 422
    assert client.get(API).json()["total"] == 0


def test_upload_without_a_file_is_422(client):
    assert client.post(f"{API}/upload", data={"tags": ["x"]}).status_code == 422


def test_oversized_upload_is_rejected_by_the_service_limit(make_client):
    small = make_client(max_upload_bytes=1024)
    response = upload(small, "big.txt", b"a" * 2000, "text/plain")
    assert response.status_code == 413 and error_code(response) == "payload_too_large"


def test_body_over_the_hard_limit_is_rejected_before_it_is_read(make_client):
    small = make_client(max_upload_bytes=1024)
    response = upload(small, "big.txt", b"a" * 1_300_000, "text/plain")
    assert response.status_code == 413


def test_oversized_json_body_is_rejected(make_client):
    small = make_client(max_text_chars=1000)
    assert small.post(f"{API}/notes", json={"content": "a" * 100_000}).status_code == 413
    assert small.post(f"{API}/notes", json={"content": "word " * 400}).status_code == 413  # too long for the service


def test_body_header_rules(settings):
    json_headers = {"content-length": "10", "content-type": "application/json"}
    check_body_headers("GET", {"content-length": "999999999999"}, settings)
    check_body_headers("POST", {}, settings)  # no body at all
    check_body_headers("POST", json_headers, settings)
    with pytest.raises(LengthRequiredError):
        check_body_headers("POST", {"transfer-encoding": "chunked"}, settings)
    with pytest.raises(InvalidInputError):
        check_body_headers("POST", {"content-length": "abc"}, settings)
    with pytest.raises(PayloadTooLargeError):
        check_body_headers("POST", {**json_headers, "content-length": str(10**9)}, settings)
    multipart = "multipart/form-data; boundary=x"
    check_body_headers("PUT", {"content-length": str(settings.max_upload_bytes), "content-type": multipart}, settings)
    with pytest.raises(PayloadTooLargeError):
        check_body_headers(
            "PUT",
            {"content-length": str(settings.max_upload_bytes + 2 * 1024 * 1024), "content-type": multipart},
            settings,
        )


def test_replace_file(client):
    source_id = upload(client, "n.txt", b"first version of the text", "text/plain").json()["source"]["id"]
    response = client.put(
        f"{API}/{source_id}/file", files={"file": ("n.txt", b"second version of the text", "text/plain")}
    )
    assert response.status_code == 200 and response.json()["created"] is False
    assert client.get(f"{API}/{source_id}/content").json()["content"] == "second version of the text"
    assert client.get(f"{API}/{source_id}/file").content == b"second version of the text"
    assert all("first" not in c["content"] for c in client.get(f"{API}/{source_id}/chunks").json())


# ======================================================================== bookmarks
def test_bookmark_is_queued_then_indexed_by_the_worker(client, fake_fetcher):
    url = "https://example.com/a"
    fake_fetcher.add_html(url, ARTICLE)
    response = client.post(f"{API}/bookmarks", json={"url": url, "tags": ["Read"]})
    assert response.status_code == 202
    assert response.json()["created"] is True
    source = response.json()["source"]
    assert source["status"] == "pending"
    assert fake_fetcher.calls == []  # the web API never downloads anything
    run_worker(client, fake_fetcher)
    done = client.get(f"{API}/{source['id']}").json()
    assert done["status"] == "completed" and done["title"] == "My Article"
    assert done["chunk_count"] >= 1 and done["fetched_at"] is not None
    assert done["tags"] == ["read"]
    again = client.post(f"{API}/bookmarks", json={"url": "https://example.com/a?utm_source=x#top"})
    assert again.status_code == 200 and again.json()["created"] is False


@pytest.mark.parametrize(
    "url", ["http://localhost/", "http://127.0.0.1/", "ftp://example.com/x", "http://example.com:8080/", "not a url"]
)
def test_dangerous_bookmarks_are_refused(client, url):
    response = client.post(f"{API}/bookmarks", json={"url": url})
    assert response.status_code == 400 and error_code(response) == "unsafe_url"
    assert client.get(API).json()["total"] == 0


def test_empty_bookmark_url_is_422(client):
    assert client.post(f"{API}/bookmarks", json={"url": ""}).status_code == 422


def test_failed_bookmark_reports_its_error(client, fake_fetcher):
    url = "https://example.com/gone"
    fake_fetcher.add(url, IngestionError("The site returned HTTP 404."))
    source_id = client.post(f"{API}/bookmarks", json={"url": url}).json()["source"]["id"]
    run_worker(client, fake_fetcher)
    detail = client.get(f"{API}/{source_id}").json()
    assert detail["status"] == "failed" and "404" in detail["error_message"]


def test_refresh_requeues_bookmarks_only(client, fake_fetcher):
    url = "https://example.com/a"
    fake_fetcher.add_html(url, ARTICLE)
    source_id = client.post(f"{API}/bookmarks", json={"url": url}).json()["source"]["id"]
    run_worker(client, fake_fetcher)
    response = client.post(f"{API}/{source_id}/refresh")
    assert response.status_code == 202 and response.json()["status"] == "pending"
    run_worker(client, fake_fetcher)
    assert client.get(f"{API}/{source_id}").json()["status"] == "completed"
    assert client.post(f"{API}/{note(client)}/refresh").status_code == 422


# =============================================================== delete / isolation
def test_delete_removes_source_chunks_and_file(client):
    source_id = upload(client).json()["source"]["id"]
    storage = client.app.state.storage
    assert len([p for p in storage.root.rglob("*") if p.is_file()]) == 1
    response = client.delete(f"{API}/{source_id}")
    assert response.status_code == 204 and response.content == b""
    for suffix in ("", "/content", "/chunks", "/file"):
        assert client.get(f"{API}/{source_id}{suffix}").status_code == 404
    assert not [p for p in storage.root.rglob("*") if p.is_file()]
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert client.delete(f"{API}/{source_id}").status_code == 404


def test_another_users_source_is_invisible(client):
    app = client.app
    with app.state.session_factory() as session:
        other = User(email="someone@example.com")
        session.add(other)
        session.commit()
        service = IngestionService(
            session, app.state.settings, storage=app.state.storage, embedder=app.state.embedder
        )
        foreign = service.create_note(other.id, "Secret", RAG_TEXT).source.id
    for method, suffix in [("get", ""), ("get", "/content"), ("get", "/chunks"), ("get", "/file"), ("delete", ""), ("post", "/refresh")]:
        response = getattr(client, method)(f"{API}/{foreign}{suffix}")
        assert response.status_code == 404, (method, suffix)
    assert client.patch(f"{API}/{foreign}", json={"title": "x"}).status_code == 404
    replacement = {"file": ("n.txt", b"text", "text/plain")}
    assert client.put(f"{API}/{foreign}/file", files=replacement).status_code == 404
    assert client.get(API).json()["total"] == 0