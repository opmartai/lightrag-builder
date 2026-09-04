from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from controller import (
    ControllerSettings,
    _advertised_identity,
    _identity,
    _lightrag_environment,
    _require_owned,
    app,
)


def test_controller_keeps_chat_and_extraction_models_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("EXTRACT_LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("EXTRACT_LLM_TIMEOUT", "120")
    monkeypatch.setenv("EXTRACT_MAX_ASYNC_LLM", "2")

    environment = _lightrag_environment("kb_test", 9621)

    assert environment["LLM_MODEL"] == "deepseek-v4-pro"
    assert environment["EXTRACT_LLM_MODEL"] == "deepseek-v4-flash"
    assert environment["EXTRACT_LLM_TIMEOUT"] == "120"
    assert environment["EXTRACT_MAX_ASYNC_LLM"] == "2"


def test_controller_namespace_isolates_identity_and_resource_ownership() -> None:
    default_identity = _identity("kb_a", 9621)
    local_identity = _identity("kb_a", 9621, "local-acceptance")
    other_identity = _identity("kb_a", 9621, "other-stack")

    assert default_identity.providerRef != local_identity.providerRef
    assert local_identity.providerRef != other_identity.providerRef
    assert local_identity.workspace != other_identity.workspace

    labels = {
        "com.amazon.experts.lightrag.managed": "true",
        "com.amazon.experts.lightrag.knowledge-base-id": "kb_a",
        "com.amazon.experts.lightrag.workspace": local_identity.workspace,
        "com.amazon.experts.lightrag.namespace": "local-acceptance",
    }
    _require_owned(labels, "kb_a", local_identity.workspace, "local-acceptance")
    with pytest.raises(RuntimeError, match="unmanaged or mismatched"):
        _require_owned(labels, "kb_a", local_identity.workspace, "other-stack")


def test_controller_keeps_container_dns_unless_host_address_is_enabled() -> None:
    identity = _identity("kb_a", 9621, "local-acceptance")
    settings = ControllerSettings(namespace="local-acceptance")

    assert _advertised_identity("kb_a", identity, settings) == identity


def test_controller_api_uses_deterministic_isolated_identity_and_narrow_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed: set[str] = set()
    cleared: list[str] = []

    def ensure(knowledge_base_id, identity, _settings) -> None:
        assert identity == _identity(knowledge_base_id, 9621)
        managed.add(knowledge_base_id)

    async def ready(_identity_value, _settings) -> None:
        return None

    def exists(knowledge_base_id, _identity_value, _namespace) -> bool:
        return knowledge_base_id in managed

    async def probe(_identity_value, _settings) -> None:
        return None

    def delete(knowledge_base_id, _port, _namespace) -> None:
        managed.discard(knowledge_base_id)

    async def clear(knowledge_base_id, _settings) -> None:
        if knowledge_base_id in managed:
            cleared.append(knowledge_base_id)

    monkeypatch.setattr("controller._ensure_container", ensure)
    monkeypatch.setattr("controller._wait_ready", ready)
    monkeypatch.setattr("controller._managed_container_exists", exists)
    monkeypatch.setattr("controller._probe", probe)
    monkeypatch.setattr("controller._clear_before_delete", clear)
    monkeypatch.setattr("controller._delete_container", delete)
    monkeypatch.setattr(
        "controller._managed_knowledge_base_ids",
        lambda _namespace: sorted(managed),
    )
    headers = {"Authorization": "Bearer change-me-controller-token"}

    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        a = client.put("/v1/instances/kb_a", headers=headers).json()
        again = client.put("/v1/instances/kb_a", headers=headers).json()
        b = client.put("/v1/instances/kb_b", headers=headers).json()
        assert a == again
        assert a["baseUrl"] != b["baseUrl"]
        assert a["workspace"] != b["workspace"]

        state = client.post(
            "/v1/instances/reconcile",
            headers=headers,
            json={"knowledgeBaseIds": ["kb_a", "kb_missing"]},
        ).json()
        assert state == {
            "managedKnowledgeBaseIds": ["kb_a", "kb_b"],
            "missingKnowledgeBaseIds": ["kb_missing"],
            "orphanKnowledgeBaseIds": ["kb_b"],
        }
        assert client.delete("/v1/instances/kb_b", headers=headers).status_code == 200
        assert client.delete("/v1/instances/kb_b", headers=headers).status_code == 200
        assert cleared == ["kb_b"]
        assert client.put("/v1/instances/not-a-kb", headers=headers).status_code == 400
