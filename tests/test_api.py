"""FastAPI TestClient tests for reliefplan.api:app.

Endpoints that call Gemini are only exercised on their error paths: with no
GEMINI_API_KEY they must return 503, and they are skipped if a key is present.
"""
import copy
import os

import pytest

httpx = pytest.importorskip("httpx")  # TestClient requires httpx

from fastapi.testclient import TestClient

import reliefplan.affinities as affinities_mod
import reliefplan.roster as roster_mod
from reliefplan.api import app
from reliefplan.cli import SAMPLE_INPUT

_HAS_GEMINI_KEY = bool(os.environ.get("GEMINI_API_KEY"))


@pytest.fixture(autouse=True)
def isolate_persistence(tmp_path, monkeypatch):
    """Keep affinities.json / roster.json writes out of the repo root."""
    monkeypatch.setattr(affinities_mod, "AFFINITIES_PATH", tmp_path / "affinities.json")
    monkeypatch.setattr(roster_mod, "ROSTER_PATH", tmp_path / "roster.json")


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sample_body():
    return copy.deepcopy(SAMPLE_INPUT)


# ---------------------------------------------------------------------------
# GET /api/sample
# ---------------------------------------------------------------------------

class TestSample:
    def test_returns_rooms_and_staff(self, client):
        resp = client.get("/api/sample")
        assert resp.status_code == 200
        data = resp.json()
        assert "rooms" in data and "staff" in data
        assert len(data["rooms"]) > 0
        assert len(data["staff"]) > 0


# ---------------------------------------------------------------------------
# POST /api/plan
# ---------------------------------------------------------------------------

class TestPlan:
    def test_sample_body_returns_plan(self, client, sample_body):
        resp = client.post("/api/plan", json=sample_body)
        assert resp.status_code == 200
        data = resp.json()
        assert "assignments" in data
        assert len(data["assignments"]) > 0
        for a in data["assignments"]:
            assert "or_id" in a and "attending" in a
        assert "unassigned_rooms" in data
        # Lookup maps keyed by string OR id / staff name
        assert set(data["room_map"]) == {str(r["id"]) for r in sample_body["rooms"]}
        assert set(data["staff_map"]) == {s["name"] for s in sample_body["staff"]}

    def test_empty_rooms_422(self, client, sample_body):
        resp = client.post("/api/plan",
                           json={"rooms": [], "staff": sample_body["staff"]})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "No rooms provided"

    def test_empty_staff_422(self, client, sample_body):
        resp = client.post("/api/plan",
                           json={"rooms": sample_body["rooms"], "staff": []})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "No staff provided"

    def test_invalid_staff_shape_422(self, client, sample_body):
        bad_staff = [{"name": "Dr. Who", "role": "wizard", "shiftType": "1-A"}]
        resp = client.post("/api/plan",
                           json={"rooms": sample_body["rooms"], "staff": bad_staff})
        assert resp.status_code == 422
        assert "role must be one of" in resp.json()["detail"]

    def test_staff_missing_required_field_422(self, client, sample_body):
        resp = client.post("/api/plan",
                           json={"rooms": sample_body["rooms"],
                                 "staff": [{"name": "Dr. Who"}]})
        assert resp.status_code == 422
        assert "missing required field 'role'" in resp.json()["detail"]

    def test_invalid_room_shape_422(self, client, sample_body):
        bad_rooms = [{"id": 44, "building": "Legacy", "floor": "L4"}]
        resp = client.post("/api/plan",
                           json={"rooms": bad_rooms, "staff": sample_body["staff"]})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/refine
# ---------------------------------------------------------------------------

class TestRefine:
    def test_missing_feedback_422(self, client):
        resp = client.post("/api/refine", json={})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "feedback is required"

    def test_blank_feedback_422(self, client):
        resp = client.post("/api/refine", json={"feedback": "   "})
        assert resp.status_code == 422

    @pytest.mark.skipif(_HAS_GEMINI_KEY, reason="GEMINI_API_KEY set — would call the LLM")
    def test_valid_feedback_without_key_503(self, client, sample_body):
        resp = client.post("/api/refine", json={
            "feedback": "Move Dr. Torres to OR 63",
            "rooms": sample_body["rooms"],
            "staff": sample_body["staff"],
            "plan": {"assignments": [], "unassigned_rooms": []},
        })
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /api/situation
# ---------------------------------------------------------------------------

class TestSituation:
    def test_missing_text_422(self, client):
        resp = client.post("/api/situation", json={})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "text is required"

    def test_blank_text_422(self, client):
        resp = client.post("/api/situation", json={"text": "  "})
        assert resp.status_code == 422

    @pytest.mark.skipif(_HAS_GEMINI_KEY, reason="GEMINI_API_KEY set — would call the LLM")
    def test_valid_text_without_key_503(self, client):
        resp = client.post("/api/situation",
                           json={"text": "OR 44 closed", "rooms": [], "staff": []})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /api/parse/* (LLM endpoints — only the non-LLM error paths)
# ---------------------------------------------------------------------------

class TestParseEndpoints:
    @pytest.mark.parametrize("path", ["/api/parse/schedule", "/api/parse/staff"])
    def test_missing_text_422(self, client, path):
        resp = client.post(path, json={"text": "  "})
        assert resp.status_code == 422

    @pytest.mark.skipif(_HAS_GEMINI_KEY, reason="GEMINI_API_KEY set — would call the LLM")
    @pytest.mark.parametrize("path", ["/api/parse/schedule", "/api/parse/staff"])
    def test_valid_text_without_key_503(self, client, path):
        resp = client.post(path, json={"text": "OR 44 Staff Patel"})
        assert resp.status_code == 503
