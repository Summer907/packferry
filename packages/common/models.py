"""Bundle v1: no API, database, registry or resolver dependencies."""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .hashes import blob_path

Status = Literal["COMPLETE", "PARTIAL", "FAILED"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Environment(Model):
    environment_id: str = Field(min_length=1, max_length=128)
    runtime: str = "cpython"
    version: str = "3.12"
    platform: str = "win_amd64"
    abi: str = "cp312"
    libc: str | None = None


class Input(Model):
    mode: Literal["packages", "project"] = "packages"
    normalized_constraints: list[str] = Field(default_factory=list)
    resolution_policy: Literal["fresh", "reuse"] = "fresh"
    lock_policy: Literal["prefer", "ignore"] = "prefer"
    original_lock_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator('normalized_constraints')
    @classmethod
    def constraints_without_secrets(cls, values):
        for value in values:
            reject_secret_urls(value)
            if re.search(r'\bfile:', value, re.IGNORECASE):
                raise ValueError('Local project paths cannot be placed in Bundle constraints')
        return values


def reject_secret_urls(value: str):
    for match in re.finditer(r'https?://[^\s\"\'<>]+', value, re.IGNORECASE):
        source = urlsplit(match.group(0))
        if source.username or source.password or source.query or source.fragment:
            raise ValueError('Bundle text cannot contain URL credentials or query/fragment secrets')
    return value


def public_source(value: str) -> str:
    # Only public provenance, never signed URLs, embedded auth or local paths.
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("Source must be an HTTP(S) URL without credentials")
    if url.query or url.fragment:
        raise ValueError("Source must not contain query/fragment secrets")
    return value


class Package(Model):
    node_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: str
    dependencies: list[str] = Field(default_factory=list)
    dependencies_known: bool = True
    _source = field_validator("source")(public_source)


class Artifact(Model):
    artifact_id: str = Field(min_length=1)
    filename: str = Field(min_length=1, max_length=255)
    type: Literal["wheel", "sdist", "npm"]
    archive_path: str
    size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str
    package_node_ids: list[str] = Field(min_length=1)
    integrity: str | None = None
    _source = field_validator("source")(public_source)

    @model_validator(mode="after")
    def validate_path(self):
        if self.archive_path != blob_path(self.sha256):
            raise ValueError("Artifact must use its canonical content-addressed path")
        if any(c in self.filename for c in '/\\\x00:') or self.filename in {".", ".."}:
            raise ValueError("Artifact filename must be a basename")
        if "packferry-bridge" in self.filename.lower() or "packferry_bridge" in self.filename.lower():
            raise ValueError("Resolver metadata carriers cannot be artifacts")
        return self


class Resolution(Model):
    environment_id: str
    nodes: list[str] = Field(default_factory=list)
    selected_artifacts: list[str] = Field(default_factory=list)
    status: Status
    warnings: list[str] = Field(default_factory=list)
    dependencies: dict[str, list[str]] = Field(default_factory=dict)
    build_dependencies: dict[str, list[str]] = Field(default_factory=dict)


class Warning(Model):
    code: str = Field(min_length=1)
    severity: Literal["info", "warning", "error"] = "warning"
    affected_environment_ids: list[str] = Field(default_factory=list)
    affected_node_ids: list[str] = Field(default_factory=list)
    message: str = Field(min_length=1)
    _message = field_validator('message')(reject_secret_urls)


class Verification(Model):
    resolution_status: Status
    collection_status: Status
    offline_install_status: Literal["NOT_VERIFIED", "VERIFIED", "BLOCKED"] = "NOT_VERIFIED"


class Manifest(Model):
    format_version: Literal[1] = 1
    bundle_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ecosystem: Literal["pypi", "npm"]
    generator_version: str = "0.1.0"
    tool_versions: dict[str, str] = Field(default_factory=dict)
    input: Input
    environments: list[Environment] = Field(min_length=1)
    packages: list[Package] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    resolutions: list[Resolution] = Field(min_length=1)
    warnings: list[Warning] = Field(default_factory=list)
    verification: Verification

    @model_validator(mode="after")
    def references(self):
        def ids(items, attr):
            values = [getattr(item, attr) for item in items]
            if len(set(values)) != len(values):
                raise ValueError(f"Duplicate {attr}")
            return set(values)

        envs = ids(self.environments, "environment_id")
        nodes = ids(self.packages, "node_id")
        artifacts = ids(self.artifacts, "artifact_id")
        if ids(self.resolutions, "environment_id") != envs:
            raise ValueError("Exactly one resolution is required per environment")
        for node in self.packages:
            if not set(node.dependencies) <= nodes:
                raise ValueError("Dangling package dependency")
        for artifact in self.artifacts:
            if not set(artifact.package_node_ids) <= nodes:
                raise ValueError("Dangling artifact package reference")
            if (artifact.type == "npm") != (self.ecosystem == "npm"):
                raise ValueError("Mixed ecosystems")
        for result in self.resolutions:
            if not set(result.nodes) <= nodes or not set(result.selected_artifacts) <= artifacts:
                raise ValueError("Dangling resolution reference")
            for graph in [result.dependencies, result.build_dependencies]:
                if not set(graph) <= set(result.nodes) or any(not set(children) <= set(result.nodes) for children in graph.values()):
                    raise ValueError('Environment graph has dangling or foreign nodes')
            selected = [a for a in self.artifacts if a.artifact_id in result.selected_artifacts]
            if any(not set(a.package_node_ids) & set(result.nodes) for a in selected):
                raise ValueError("Selected artifact is unrelated to environment nodes")
            if result.status == "COMPLETE" and any(
                not p.dependencies_known for p in self.packages if p.node_id in result.nodes
            ):
                raise ValueError("Unknown dependency graph cannot be COMPLETE")
        for warning in self.warnings:
            if not set(warning.affected_environment_ids) <= envs or not set(warning.affected_node_ids) <= nodes:
                raise ValueError("Dangling warning reference")
        if self.verification.resolution_status == "COMPLETE" and any(
            r.status != "COMPLETE" for r in self.resolutions
        ):
            raise ValueError("Global resolution status contradicts environments")
        if self.verification.collection_status == "COMPLETE" and any(
            not any(p in a.package_node_ids for a in self.artifacts) for p in nodes
        ):
            raise ValueError("COMPLETE collection requires an artifact for every node")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include timezone")
        return self
