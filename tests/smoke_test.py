import os
import time
import uuid

import requests

API_URL = os.getenv("API_URL", "http://localhost:8000")
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASS = "securepassword"

def run_smoke_test():
    print(f"Starting smoke test against {API_URL}")

    # 1. Health / Readiness
    resp = requests.get(f"{API_URL}/health")
    assert resp.status_code == 200, f"Healthcheck failed: {resp.text}"
    print("✓ Healthcheck passed.")

    resp = requests.get(f"{API_URL}/health/ready")
    assert resp.status_code == 200, f"Readiness failed: {resp.text}"
    print("✓ Readiness passed.")

    # 2. Login
    resp = requests.post(
        f"{API_URL}/auth/admin/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}
    )
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("✓ Admin login passed.")

    # 3. Upload & Ingest Test Doc
    with open("tests/test_doc.pdf", "wb") as f:
        f.write(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>\nendobj\n4 0 obj\n<< /Length 53 >>\nstream\nBT\n/F1 12 Tf\n100 700 Td\n(This is a test document for NPCI policies.) Tj\nET\nendstream\nendobj\nxref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000289 00000 n \ntrailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n393\n%%EOF")

    with open("tests/test_doc.pdf", "rb") as f:
        resp = requests.post(f"{API_URL}/admin/upload", files={"file": f}, headers=headers)
    assert resp.status_code == 200, f"Upload failed: {resp.text}"
    print("✓ Document uploaded.")

    resp = requests.post(f"{API_URL}/ingest", headers=headers)
    assert resp.status_code == 200, f"Ingestion failed: {resp.text}"
    print("✓ Ingestion triggered.")

    time.sleep(2)  # Wait for ingestion (inline task or worker process)

    # 4. Chat
    session_id = str(uuid.uuid4())
    resp = requests.post(
        f"{API_URL}/chat",
        json={"session_id": session_id, "question": "What is this document about?"},
        headers=headers
    )
    assert resp.status_code == 200, f"Chat failed: {resp.text}"
    data = resp.json()
    assert "answer" in data
    print("✓ Chat response received.")

    # 5. Escalate (Feedback)
    msg_id = data.get("message_id")
    if msg_id:
        resp = requests.post(
            f"{API_URL}/feedback/{msg_id}",
            json={"rating": "negative", "reason": "incomplete"},
            headers=headers
        )
        assert resp.status_code == 200, f"Feedback failed: {resp.text}"
        print("✓ Feedback submitted.")

    # 6. Check Metrics
    resp = requests.get(f"{API_URL}/metrics", headers=headers)
    assert resp.status_code == 200, f"Metrics failed: {resp.status_code}"
    assert "http_requests_total" in resp.text
    print("✓ Metrics accessible.")

    print("Smoke test completed successfully!")

if __name__ == "__main__":
    run_smoke_test()
