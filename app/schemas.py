"""Request/response shapes for the planner, documented for OpenAPI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TraceStep(BaseModel):
    name: str
    zone: str | None
    outcome: Literal[
        "cname", "a", "nodata", "nxdomain", "external", "loop", "too_long"
    ]
    target: str | None = None
    addresses: list[str] = Field(default_factory=list)


class ChainFailure(BaseModel):
    domain: str
    resolved: bool
    addresses: list[str]
    category: Literal[
        "missing_record", "cname_loop", "external_target", "chain_too_long"
    ]
    category_label: str | None
    detail: str | None = None
    message: str
    chain: str
    trace: list[TraceStep]


class FailureSummary(BaseModel):
    total: int
    by_category: dict[str, int]
    by_category_label: dict[str, int]


class PlanStep(BaseModel):
    step: int
    zone: str
    action: str
    addresses_before: dict[str, list[str]]
    addresses_after: dict[str, list[str]]
    all_required_resolvable: bool


class BlockedZone(BaseModel):
    zone: str
    categories: list[dict[str, str]]
    failures: list[ChainFailure]


class PlanResponse(BaseModel):
    mode: Literal["authoritative_no_cache"]
    zone_count: int
    domain_count: int
    assumptions: list[str]
    status: Literal["already_broken", "safe_order_found", "no_safe_order"]
    status_meaning: str
    message: str
    order: list[str]
    safe_prefix: list[str]
    safe_prefix_is_maximal: bool | None = None
    steps: list[PlanStep]
    final_state_resolvable: bool | None = None
    fault_chains: list[ChainFailure]
    blocked_zones: list[BlockedZone]
    failure_summary: FailureSummary

    model_config = {"extra": "allow"}


class HealthResponse(BaseModel):
    status: str
    version: str


class SnapshotRecord(BaseModel):
    """One record of a reconstructed snapshot (same shape as /plan records)."""

    name: str
    type: Literal["A", "CNAME"]
    value: str
    ttl: int


class SnapshotDifference(BaseModel):
    """One record on which two valid chains disagree."""

    record: SnapshotRecord
    present_in_chain_a: bool
    present_in_chain_b: bool


class IxfrReconstructResponse(BaseModel):
    status: Literal["unique_snapshot", "ambiguous", "unreachable"]
    status_meaning: str
    message: str
    zone: str
    backup_serial: int
    target_serial: int
    delta_count: int
    assumptions: list[str]
    # unique_snapshot only
    records: list[SnapshotRecord] | None = None
    record_count: int | None = None
    example_chain: list[str | int] | None = None
    # ambiguous only
    chain_a: list[str | int] | None = None
    chain_b: list[str | int] | None = None
    difference: SnapshotDifference | None = None
    distinct_snapshots_at_target: int | None = None
    # unreachable only
    highest_reachable_serial: int | None = None
    reachable_serials: list[int] | None = None

    model_config = {"extra": "allow"}
