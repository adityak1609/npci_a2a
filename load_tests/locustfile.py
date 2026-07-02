import uuid
from locust import HttpUser, task, between
import json

class PolicyBotUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        """On start, the user attempts to log in to get the auth cookie."""
        self.session_id = str(uuid.uuid4())
        
        # We assume the environment is pre-seeded with this test admin
        response = self.client.post(
            "/auth/admin/login",
            json={"email": "admin@example.com", "password": "securepassword"}
        )
        if response.status_code != 200:
            print("Warning: Admin login failed during load test start.")

    @task(3)
    def chat_query(self):
        """Simulate a user asking a question."""
        payload = {
            "session_id": self.session_id,
            "question": "What is the policy for A2A data handling?",
        }
        self.client.post("/chat", json=payload, name="/chat")

    @task(1)
    def view_dashboard(self):
        """Simulate an admin checking the dashboard."""
        self.client.get("/admin/dashboard", name="/admin/dashboard")

    @task(1)
    def view_pdfs(self):
        """Simulate an admin checking the uploaded PDFs list."""
        self.client.get("/admin/pdfs", name="/admin/pdfs")
