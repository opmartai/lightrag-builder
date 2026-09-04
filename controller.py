from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
from functools import lru_cache
from typing import Any

import httpx
import docker
from docker.errors import NotFound
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_MANAGED_LABEL = "com.amazon.experts.lightrag.managed"
_KB_LABEL = "com.amazon.experts.lightrag.knowledge-base-id"
_WORKSPACE_LABEL = "com.amazon.experts.lightrag.workspace"
_VOLUME_LABEL = "com.amazon.experts.lightrag.volume"
_NAMESPACE_LABEL = "com.amazon.experts.lightrag.namespace"
_LOCKS: dict[str, asyncio.Lock] = {}
_LIGHTRAG_156_IMAGE = (
    "ghcr.io/hkuds/lightrag@sha256:ab23a9c83a735901b18c8960b6b482b602d5b6291abb7e07c5776f7bb2da504e"
)


class ControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIGHTRAG_CONTROLLER_", extra="ignore")

    token: str = "change-me-controller-token"
    image: str = _LIGHTRAG_156_IMAGE
    network: str = "experts-stack_default"
    port: int = 9621
    namespace: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9_-]{0,63})?$")
    advertise_container_ip: bool = False
    health_timeout_seconds: float = Field(default=180.0, gt=0, le=600)


class InstanceResponse(BaseModel):
    provider: str = "docker"
    providerRef: str
    baseUrl: str
    workspace: str
    status: str


class ReconcileRequest(BaseModel):
    knowledgeBaseIds: list[str] = Field(default_factory=list, max_length=1000)


class ReconcileResponse(BaseModel):
    managedKnowledgeBaseIds: list[str]
    missingKnowledgeBaseIds: list[str]
    orphanKnowledgeBaseIds: list[str]


@lru_cache
def get_controller_settings() -> ControllerSettings:
    return ControllerSettings()


def _authorize(
    authorization: str | None = Header(default=None),
    settings: ControllerSettings = Depends(get_controller_settings),
) -> None:
    expected = f"Bearer {settings.token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Controller authentication required")


app = FastAPI(
    title="LightRAG Controller",
    version="1",
    description=(
        "Idempotent per-knowledge-base LightRAG lifecycle boundary. "
        "It intentionally exposes no generic Docker operations."
    ),
    dependencies=[Depends(_authorize)],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "lightrag-controller"}


@app.put("/v1/instances/{knowledge_base_id}", response_model=InstanceResponse)
async def ensure_instance(
    knowledge_base_id: str,
    settings: ControllerSettings = Depends(get_controller_settings),
) -> InstanceResponse:
    _validate_kb_id(knowledge_base_id)
    lock = _LOCKS.setdefault(knowledge_base_id, asyncio.Lock())
    async with lock:
        identity = _identity(knowledge_base_id, settings.port, settings.namespace)
        try:
            await asyncio.to_thread(_ensure_container, knowledge_base_id, identity, settings)
            await _wait_ready(identity, settings)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"LightRAG provisioning failed: {type(exc).__name__}",
            ) from exc
        return await asyncio.to_thread(_advertised_identity, knowledge_base_id, identity, settings)


@app.get("/v1/instances/{knowledge_base_id}", response_model=InstanceResponse)
async def instance_health(
    knowledge_base_id: str,
    settings: ControllerSettings = Depends(get_controller_settings),
) -> InstanceResponse:
    _validate_kb_id(knowledge_base_id)
    identity = _identity(knowledge_base_id, settings.port, settings.namespace)
    try:
        exists = await asyncio.to_thread(
            _managed_container_exists,
            knowledge_base_id,
            identity,
            settings.namespace,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"LightRAG health check failed: {type(exc).__name__}",
        ) from exc
    if not exists:
        raise HTTPException(status_code=404, detail="Managed LightRAG instance not found")
    await _probe(identity, settings)
    return await asyncio.to_thread(_advertised_identity, knowledge_base_id, identity, settings)


@app.delete("/v1/instances/{knowledge_base_id}")
async def delete_instance(
    knowledge_base_id: str,
    settings: ControllerSettings = Depends(get_controller_settings),
) -> dict[str, str]:
    _validate_kb_id(knowledge_base_id)
    lock = _LOCKS.setdefault(knowledge_base_id, asyncio.Lock())
    async with lock:
        try:
            await _clear_before_delete(knowledge_base_id, settings)
            await asyncio.to_thread(
                _delete_container,
                knowledge_base_id,
                settings.port,
                settings.namespace,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"LightRAG deletion failed: {type(exc).__name__}",
            ) from exc
    return {"status": "deleted"}


@app.post("/v1/instances/reconcile", response_model=ReconcileResponse)
async def reconcile_instances(
    request: ReconcileRequest,
    settings: ControllerSettings = Depends(get_controller_settings),
) -> ReconcileResponse:
    requested = set(request.knowledgeBaseIds)
    for knowledge_base_id in requested:
        _validate_kb_id(knowledge_base_id)
    managed = set(await asyncio.to_thread(_managed_knowledge_base_ids, settings.namespace))
    return ReconcileResponse(
        managedKnowledgeBaseIds=sorted(managed),
        missingKnowledgeBaseIds=sorted(requested - managed),
        orphanKnowledgeBaseIds=sorted(managed - requested),
    )


def _validate_kb_id(knowledge_base_id: str) -> None:
    if not re.fullmatch(r"kb_[a-zA-Z0-9_-]{1,120}", knowledge_base_id):
        raise HTTPException(status_code=400, detail="Invalid knowledge base id")


def _identity(knowledge_base_id: str, port: int, namespace: str = "") -> InstanceResponse:
    identity_key = knowledge_base_id if not namespace else f"{namespace}\0{knowledge_base_id}"
    digest = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()
    workspace = f"kb_{digest[:24]}"
    name = f"experts-lightrag-{digest[:20]}"
    return InstanceResponse(
        providerRef=name,
        baseUrl=f"http://{name}:{port}",
        workspace=workspace,
        status="ready",
    )


def _ensure_container(
    knowledge_base_id: str,
    identity: InstanceResponse,
    settings: ControllerSettings,
) -> None:
    client = docker.from_env()
    name = identity.providerRef
    volume_name = f"{name}-data"
    labels = {
        _MANAGED_LABEL: "true",
        _KB_LABEL: knowledge_base_id,
        _WORKSPACE_LABEL: identity.workspace,
    }
    if settings.namespace:
        labels[_NAMESPACE_LABEL] = settings.namespace
    try:
        container = client.containers.get(name)
        _require_owned(
            container.labels,
            knowledge_base_id,
            identity.workspace,
            settings.namespace,
        )
    except NotFound:
        try:
            client.images.get(settings.image)
        except NotFound:
            client.images.pull(settings.image)
        try:
            volume = client.volumes.get(volume_name)
            _require_owned(
                volume.attrs.get("Labels") or {},
                knowledge_base_id,
                identity.workspace,
                settings.namespace,
            )
        except NotFound:
            volume = client.volumes.create(
                name=volume_name,
                labels={**labels, _VOLUME_LABEL: "true"},
            )
        container = client.containers.create(
            settings.image,
            name=name,
            detach=True,
            environment=_lightrag_environment(identity.workspace, settings.port),
            labels=labels,
            network=settings.network,
            volumes={volume.name: {"bind": "/app/data/rag_storage", "mode": "rw"}},
            restart_policy={"Name": "unless-stopped"},
        )
    container.reload()
    if container.status != "running":
        container.start()


def _advertised_identity(
    knowledge_base_id: str,
    identity: InstanceResponse,
    settings: ControllerSettings,
) -> InstanceResponse:
    """Return a Backend-reachable address without weakening resource ownership checks.

    Container DNS remains the stable default. A host-native Backend cannot resolve those names,
    so a local deployment may opt into the container address on the Controller-managed bridge.
    The address is refreshed by every ensure and health response.
    """
    if not settings.advertise_container_ip:
        return identity
    client = docker.from_env()
    container = client.containers.get(identity.providerRef)
    _require_owned(
        container.labels,
        knowledge_base_id,
        identity.workspace,
        settings.namespace,
    )
    container.reload()
    networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    network = networks.get(settings.network) or {}
    address = str(network.get("IPAddress") or "").strip()
    if not address:
        raise RuntimeError("Managed LightRAG container has no address on its configured network")
    return identity.model_copy(update={"baseUrl": f"http://{address}:{settings.port}"})


def _managed_container_exists(
    knowledge_base_id: str,
    identity: InstanceResponse,
    namespace: str = "",
) -> bool:
    client = docker.from_env()
    try:
        container = client.containers.get(identity.providerRef)
    except NotFound:
        return False
    _require_owned(container.labels, knowledge_base_id, identity.workspace, namespace)
    container.reload()
    return container.status == "running"


def _managed_resource_exists(
    knowledge_base_id: str,
    identity: InstanceResponse,
    namespace: str = "",
) -> bool:
    """Return whether the exact managed container or its owned volume exists."""
    client = docker.from_env()
    try:
        container = client.containers.get(identity.providerRef)
        _require_owned(container.labels, knowledge_base_id, identity.workspace, namespace)
        return True
    except NotFound:
        pass
    try:
        volume = client.volumes.get(f"{identity.providerRef}-data")
    except NotFound:
        return False
    _require_owned(
        volume.attrs.get("Labels") or {},
        knowledge_base_id,
        identity.workspace,
        namespace,
    )
    return True


def _delete_container(knowledge_base_id: str, port: int, namespace: str = "") -> None:
    client = docker.from_env()
    identity = _identity(knowledge_base_id, port, namespace)
    volume_name = f"{identity.providerRef}-data"
    try:
        container = client.containers.get(identity.providerRef)
    except NotFound:
        container = None
    if container is not None:
        _require_owned(container.labels, knowledge_base_id, identity.workspace, namespace)
        container.remove(force=True, v=True)
    try:
        volume = client.volumes.get(volume_name)
    except NotFound:
        return
    _require_owned(
        volume.attrs.get("Labels") or {},
        knowledge_base_id,
        identity.workspace,
        namespace,
    )
    volume.remove(force=False)


def _managed_knowledge_base_ids(namespace: str = "") -> list[str]:
    client = docker.from_env()
    containers = client.containers.list(all=True, filters={"label": f"{_MANAGED_LABEL}=true"})
    volumes = client.volumes.list(filters={"label": f"{_MANAGED_LABEL}=true"})
    return sorted(
        {
            str(container.labels.get(_KB_LABEL))
            for container in containers
            if container.labels.get(_KB_LABEL)
            and (container.labels.get(_NAMESPACE_LABEL) or "") == namespace
        }
        | {
            str((volume.attrs.get("Labels") or {}).get(_KB_LABEL))
            for volume in volumes
            if (volume.attrs.get("Labels") or {}).get(_KB_LABEL)
            and ((volume.attrs.get("Labels") or {}).get(_NAMESPACE_LABEL) or "") == namespace
        }
    )


def _require_owned(
    labels: dict[str, str],
    knowledge_base_id: str,
    workspace: str,
    namespace: str = "",
) -> None:
    if (
        labels.get(_MANAGED_LABEL) != "true"
        or labels.get(_KB_LABEL) != knowledge_base_id
        or labels.get(_WORKSPACE_LABEL) != workspace
        or (labels.get(_NAMESPACE_LABEL) or "") != namespace
    ):
        raise RuntimeError("Refusing to operate on an unmanaged or mismatched Docker resource")


def _lightrag_environment(workspace: str, port: int) -> dict[str, str]:
    environment = {
        "HOST": "0.0.0.0",
        "PORT": str(port),
        "WORKSPACE": workspace,
        "LIGHTRAG_KV_STORAGE": "PGKVStorage",
        "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
        "LIGHTRAG_GRAPH_STORAGE": "PGTableGraphStorage",
        "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage",
        "POSTGRES_VECTOR_INDEX_TYPE": os.getenv("POSTGRES_VECTOR_INDEX_TYPE", "HNSW_HALFVEC"),
        "POSTGRES_HOST": os.getenv("POSTGRES_HOST", "postgres"),
        "POSTGRES_PORT": os.getenv("POSTGRES_PORT", "5432"),
        "POSTGRES_USER": os.getenv("POSTGRES_USER", "postgres"),
        "POSTGRES_DATABASE": os.getenv("POSTGRES_DATABASE", "lightrag"),
        "LLM_BINDING": os.getenv("LLM_BINDING", "openai"),
        "EMBEDDING_BINDING": os.getenv("EMBEDDING_BINDING", "openai"),
    }
    passthrough = (
        "LIGHTRAG_API_KEY",
        "POSTGRES_PASSWORD",
        "LLM_BINDING_HOST",
        "LLM_BINDING_API_KEY",
        "LLM_MODEL",
        "EXTRACT_LLM_MODEL",
        "EXTRACT_LLM_TIMEOUT",
        "EXTRACT_MAX_ASYNC_LLM",
        "EMBEDDING_BINDING_HOST",
        "EMBEDDING_BINDING_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIM",
        "RERANK_BINDING",
        "RERANK_BINDING_HOST",
        "RERANK_BINDING_API_KEY",
        "RERANK_MODEL",
        "LLM_TIMEOUT",
        "KEYWORD_LLM_TIMEOUT",
        "QUERY_LLM_TIMEOUT",
        "MAX_ASYNC_LLM",
        "EMBEDDING_FUNC_MAX_ASYNC",
        "MAX_PARALLEL_INSERT",
    )
    for name in passthrough:
        value = os.getenv(name)
        if value:
            environment[name] = value
    return environment


async def _wait_ready(identity: InstanceResponse, settings: ControllerSettings) -> None:
    deadline = asyncio.get_running_loop().time() + settings.health_timeout_seconds
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            await _probe(identity, settings)
            return
        except Exception as exc:  # noqa: BLE001 - bounded readiness polling
            last_error = exc
            await asyncio.sleep(1)
    raise HTTPException(
        status_code=503,
        detail=f"LightRAG instance did not become ready: {type(last_error).__name__}",
    )


async def _clear_before_delete(knowledge_base_id: str, settings: ControllerSettings) -> None:
    """Clear PostgreSQL-backed workspace data before destroying local resources.

    Deleting only a container and volume is insufficient because dynamic LightRAG instances use
    PostgreSQL storage. An owned orphan is recreated/started if necessary, then the authenticated
    complete-clear endpoint must succeed. Busy or partial cleanup keeps the instance intact so the
    Controller delete remains safely retryable.
    """
    identity = _identity(knowledge_base_id, settings.port, settings.namespace)
    exists = await asyncio.to_thread(
        _managed_resource_exists,
        knowledge_base_id,
        identity,
        settings.namespace,
    )
    if not exists:
        return
    await asyncio.to_thread(_ensure_container, knowledge_base_id, identity, settings)
    await _wait_ready(identity, settings)
    headers = {}
    api_key = os.getenv("LIGHTRAG_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    async with httpx.AsyncClient(
        timeout=settings.health_timeout_seconds, trust_env=False
    ) as client:
        response = await client.request("DELETE", f"{identity.baseUrl}/documents", headers=headers)
    if response.status_code == 409:
        raise HTTPException(status_code=503, detail="LightRAG workspace is busy")
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    if str(payload.get("status") or "").lower() != "success":
        raise HTTPException(
            status_code=503,
            detail="LightRAG workspace clear was not completely successful",
        )


async def _probe(identity: InstanceResponse, settings: ControllerSettings) -> None:
    headers = {}
    api_key = os.getenv("LIGHTRAG_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(f"{identity.baseUrl}/health", headers=headers)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    configuration = payload.get("configuration")
    actual_workspace = configuration.get("workspace") if isinstance(configuration, dict) else None
    if actual_workspace is not None and actual_workspace != identity.workspace:
        raise RuntimeError("LightRAG health workspace does not match the managed identity")
    if str(payload.get("status") or "").lower() not in {"healthy", "ok"}:
        raise RuntimeError("LightRAG health status is not ready")
