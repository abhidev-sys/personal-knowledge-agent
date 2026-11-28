"""Manual end-to-end check of the HTTP API against a RUNNING server.

Terminal 1 (server):  & $py -m uvicorn --factory app.main:app_factory --port 8000
Terminal 2 (worker):  & $py scripts/run_worker.py
Terminal 3 (this):    & $py scripts/try_api.py            (add --keep to keep the demo data)
"""
import sys
import time
import uuid

import httpx

BASE = "http://localhost:8000"
API = f"{BASE}/api/v1/sources"
MARKDOWN = b"# Agents\nTools let a model act.\n\n## Memory\nAgents can remember user preferences.\n"


def show(label: str, response: httpx.Response) -> dict:
    body = response.json() if response.content else {}
    print(f"{label:<28} HTTP {response.status_code}")
    return body


def main(keep: bool) -> int:
    run = uuid.uuid4().hex[:6]
    created: list[str] = []
    ok = True
    with httpx.Client(timeout=60) as client:
        ready = client.get(f"{BASE}/ready")
        print("ready:", ready.status_code, ready.json())

        note = show(
            "create note",
            client.post(
                f"{API}/notes",
                json={
                    "title": f"RAG idea {run}",
                    "content": "Retrieval augmented generation lets a model answer from my notes.",
                    "tags": ["rag", "demo"],
                },
            ),
        )
        created.append(note["source"]["id"])
        same = show(
            "same note again",
            client.post(
                f"{API}/notes",
                json={
                    "title": f"RAG idea {run}",
                    "content": "Retrieval augmented generation lets a model answer from my notes.",
                },
            ),
        )
        print("   created:", note["created"], "| again:", same["created"])

        upload = show(
            "upload markdown",
            client.post(
                f"{API}/upload",
                files={"file": (f"agents-{run}.md", MARKDOWN + run.encode(), "text/markdown")},
                data={"tags": ["demo", "agents"]},
            ),
        )
        created.append(upload["source"]["id"])

        bookmark = show(
            "bookmark example.com",
            client.post(f"{API}/bookmarks", json={"url": "https://example.com", "tags": ["demo"]}),
        )
        bookmark_id = bookmark["source"]["id"]
        if bookmark["created"]:
            created.append(bookmark_id)

        print("waiting for the worker (up to 90 s)...")
        status = bookmark["source"]["status"]
        deadline = time.monotonic() + 90
        while status not in ("completed", "failed") and time.monotonic() < deadline:
            time.sleep(2)
            status = client.get(f"{API}/{bookmark_id}").json()["status"]
        if status != "completed":
            ok = False
            print(f"   bookmark is '{status}'. Is `scripts/run_worker.py` running in another terminal?")

        listing = client.get(API).json()
        print(f"\n{'type':10} {'status':10} {'chunks':>6}  title")
        for item in listing["items"]:
            print(f"{item['source_type']:10} {item['status']:10} {item['chunk_count']:>6}  {item['title']}")

        chunks = client.get(f"{API}/{note['source']['id']}/chunks").json()
        print("\nfirst chunk of the note:", chunks[0]["content"][:70], "| has_embedding:", chunks[0]["has_embedding"])

        if not keep:
            for source_id in created:
                client.delete(f"{API}/{source_id}")
            print("\n(demo data removed; use --keep to keep it)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main("--keep" in sys.argv))