"""End-to-end API tests (offline): session + CSRF, cases, upload → index → retrieve,
matter isolation, streamed chat with sources and citation audit."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from nyaya import retrieval
from nyaya.server import app


@pytest.fixture
def client(fresh_db):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        s = c.get("/api/session").json()
        c.headers.update({"X-Nyaya-CSRF": s["csrf"]})
        yield c


def _wait_ready(client, doc_id, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        d = client.get(f"/api/documents/{doc_id}").json()
        if d["status"] != "processing":
            return d
        time.sleep(0.1)
    raise AssertionError("ingestion timed out")


def _case(client, title):
    r = client.post("/api/cases", json={"title": title, "case_type": "Civil", "client_name": "A"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(client, case_id, name, text):
    r = client.post(f"/api/cases/{case_id}/documents", files={"file": (name, text.encode(), "text/plain")},
                    data={"doc_type": "contract"})
    assert r.status_code == 200, r.text
    return _wait_ready(client, r.json()["id"])


LEASE = ("LEASE AGREEMENT\n1. The tenant shall pay a security deposit of rupees two lakh.\n"
         "2. The security deposit is refundable within thirty days of vacating the premises.\n"
         "3. Rent of rupees forty thousand is payable monthly.\n4. Either party may terminate on three months notice.\n"
         "5. Disputes shall be referred to arbitration at Pune.\n6. The lessee shall not sublet the premises.\n")


def test_security_controls(fresh_db):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get("/api/cases").status_code == 401                       # no session
        c.get("/api/session")
        assert c.get("/api/cases").status_code == 200
        assert c.post("/api/cases", json={"title": "x"}).status_code == 403  # missing CSRF header
    with TestClient(app, base_url="http://evil.example.com") as c:
        assert c.get("/").status_code == 400                                 # DNS-rebinding host blocked
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        r = c.get("/")
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert "httponly" in r.headers.get("set-cookie", "").lower()


def test_upload_validation(client):
    cid = _case(client, "Validation")
    r = client.post(f"/api/cases/{cid}/documents", files={"file": ("x.exe", b"MZ..", "application/octet-stream")})
    assert r.status_code == 415
    r = client.post(f"/api/cases/{cid}/documents", files={"file": ("fake.pdf", b"MZ not a pdf", "application/pdf")})
    assert r.status_code == 415


def test_upload_index_retrieve_and_isolation(client):
    a = _case(client, "Tenant matter")
    b = _case(client, "Other matter")
    doc = _upload(client, a, "lease.txt", LEASE)
    assert doc["status"] == "ready" and doc["n_chunks"] >= 1
    dup = client.post(f"/api/cases/{a}/documents", files={"file": ("lease-copy.txt", LEASE.encode(), "text/plain")})
    assert dup.json().get("duplicate") is True

    hits, diag = retrieval.search("is the security deposit refundable", scope="case", case_id=a)
    assert hits and "refundable" in " ".join(h.text for h in hits)
    hits_b, _ = retrieval.search("is the security deposit refundable", scope="case", case_id=b)
    assert hits_b == []                                   # case B can never see case A's documents
    hits_lib, _ = retrieval.search("security deposit refundable", scope="library", case_id=a)
    assert all(h.collection != "case" for h in hits_lib)

    assert client.delete(f"/api/documents/{doc['id']}").status_code == 200
    hits, _ = retrieval.search("security deposit refundable", scope="case", case_id=a)
    assert hits == []


def _events(resp):
    return [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: ")]


def test_chat_stream_research_with_sources_and_audit(client):
    cid = _case(client, "Tenant matter")
    _upload(client, cid, "lease.txt", LEASE)
    r = client.post("/api/chat", json={"message": "Is the security deposit refundable?", "case_id": cid, "scope": "case"})
    assert r.status_code == 200
    ev = _events(r)
    types = [e["type"] for e in ev]
    assert types[0] == "chat" and "sources" in types and "token" in types and types[-1] == "done"
    assert any(e["type"] == "verify" and e["grounded"] for e in ev)
    chat_id = ev[0]["chat_id"]
    msgs = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["sources"]


def test_chat_calculation_is_deterministic(client):
    r = client.post("/api/chat", json={"message": "limitation for breach of contract broken on 2024-03-15"})
    text = "".join(e.get("text", "") for e in _events(r) if e["type"] == "token")
    assert "2027-03-15" in text and "Art. 55" in text


def test_chat_abstains_without_evidence(client):
    cid = _case(client, "Empty matter")
    r = client.post("/api/chat", json={"message": "What did the court hold about easements?", "case_id": cid, "scope": "case"})
    text = "".join(e.get("text", "") for e in _events(r) if e["type"] == "token")
    assert "won't guess" in text
