"""Reconstruct a unique single-zone snapshot from out-of-order IXFR fragments.

A DNS duty operator holds IXFR logs collected from several replicas.  The
fragments arrive out of order and may overlap or contradict one another.
Given the zone name, an A/CNAME backup together with its SOA serial, the
master's target serial, and up to 20 fragments (each with a unique id, a
from/to serial pair and the deleted/added records), this module determines
whether the fragments can be chained into a *unique* snapshot of the zone at
the target serial.

Chain rules (serials never wrap, so plain integer ordering is used; deltas
only move forward, i.e. ``to_serial > from_serial``):

* a fragment applies to a state iff its ``from_serial`` equals the state's
  serial (fragments chain by serial);
* every deleted record must match a current record exactly, TTL included;
* no added record may duplicate a record that survives the deletions;
* after a whole fragment is applied, the zone records must satisfy the same
  rules /plan enforces (self-contained names, A/CNAME exclusivity, no apex
  CNAME, consistent CNAME targets, nothing below a CNAME, and the per-zone
  record limit so the snapshot can be fed back into /plan).

Because serials strictly increase along every edge, the fragments form a DAG;
all valid chains are explored with memoisation over ``(serial, record set)``
states, which keeps the walk tiny for the allowed 20 fragments.

Three outcomes:

* ``unique_snapshot`` - every valid chain reaching the target yields the same
  record set (order-insensitive); the snapshot is returned as ``records``
  ready to be dropped into a /plan request;
* ``ambiguous`` - valid chains reach the target with different results; two
  witness chains (fragment ids) and one differing record are reported, and no
  snapshot is returned;
* ``unreachable`` - no valid chain reaches the target; the highest serial
  reachable before the target is reported, and no snapshot is returned.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from .models import (
    MAX_RECORDS_PER_ZONE,
    Record,
    check_zone_records,
    validate_name,
)

MAX_DELTAS = 20
MAX_SERIAL = 4_294_967_295  # SOA serials are uint32; they never wrap here.

DeltaId = Union[str, int]

STATUS_MEANINGS = {
    "unique_snapshot": "所有有效链在目标序列号处得到同一组记录",
    "ambiguous": "存在多条有效链，到达目标序列号时记录不一致",
    "unreachable": "没有任何有效链到达目标序列号",
}

ASSUMPTIONS = [
    "序列号不回绕，按普通整数比较；差量只能向前（to_serial > from_serial）。",
    "差量按序列号衔接：from_serial 必须等于当前状态的序列号。",
    "删除按完整记录匹配（名称、类型、值、TTL 全部一致）；"
    "添加不得重复删除后仍然保留的记录。",
    "每应用完一整条差量，区记录必须满足 /plan 的校验规则"
    "（含每区记录数上限），否则该差量不可用。",
    "记录按集合处理（忽略顺序）；只有所有到达目标序列号的有效链"
    "得到同一组记录时才返回快照。",
]


class IxfrDelta(BaseModel):
    """One IXFR fragment: records deleted and added between two serials."""

    id: DeltaId
    from_serial: int = Field(ge=0, le=MAX_SERIAL)
    to_serial: int = Field(ge=0, le=MAX_SERIAL)
    deletions: list[Record] = Field(
        default_factory=list, max_length=MAX_RECORDS_PER_ZONE
    )
    additions: list[Record] = Field(
        default_factory=list, max_length=MAX_RECORDS_PER_ZONE
    )

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: DeltaId) -> DeltaId:
        if isinstance(v, str) and not v.strip():
            raise ValueError("delta id must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _forward_only(self) -> "IxfrDelta":
        if self.to_serial <= self.from_serial:
            raise ValueError(
                f"delta {self.id!r} must move forward: to_serial "
                f"({self.to_serial}) must be greater than from_serial "
                f"({self.from_serial})"
            )
        return self


class IxfrReconstructRequest(BaseModel):
    """Backup plus out-of-order fragments to chain up to the target serial."""

    zone: str
    backup_serial: int = Field(ge=0, le=MAX_SERIAL)
    backup_records: list[Record] = Field(
        default_factory=list, max_length=MAX_RECORDS_PER_ZONE
    )
    target_serial: int = Field(ge=0, le=MAX_SERIAL)
    deltas: list[IxfrDelta] = Field(default_factory=list, max_length=MAX_DELTAS)

    @field_validator("zone")
    @classmethod
    def _valid_zone(cls, v: str) -> str:
        return validate_name(v, "zone name")

    @model_validator(mode="after")
    def _check_request(self) -> "IxfrReconstructRequest":
        if self.target_serial < self.backup_serial:
            raise ValueError(
                f"target_serial ({self.target_serial}) must not be earlier "
                f"than backup_serial ({self.backup_serial})"
            )
        ids = [d.id for d in self.deltas]
        if len(set(ids)) != len(ids):
            raise ValueError("delta ids must be unique")
        # The backup is the starting state of every chain, so it must already
        # satisfy the /plan zone rules.
        check_zone_records(self.zone, self.backup_records, field="backup_records")
        return self


class RecKey(NamedTuple):
    """Canonical identity of a record: a deletion matches all four fields."""

    name: str
    type: str
    value: str
    ttl: int


def _key(rec: Record) -> RecKey:
    return RecKey(rec.name, rec.type, rec.value, rec.ttl)


def _record_json(key: RecKey) -> dict[str, Any]:
    return {"name": key.name, "type": key.type, "value": key.value, "ttl": key.ttl}


def _apply_delta(delta: IxfrDelta, state: frozenset[RecKey]) -> frozenset[RecKey] | None:
    """Apply one whole fragment to ``state``; None when it does not apply.

    A fragment applies iff every deleted record is present (full match, TTL
    included) and no added record duplicates a record surviving the deletions.
    Records are a set, so duplicate entries inside one section collapse.
    """
    deleted = {_key(r) for r in delta.deletions}
    if not deleted.issubset(state):
        return None
    survivors = state - deleted
    added = {_key(r) for r in delta.additions}
    if added & survivors:
        return None
    return survivors | added


def _zone_state_valid(zone: str, records: frozenset[RecKey]) -> bool:
    """The /plan zone rules, plus the per-zone record limit /plan enforces."""
    if len(records) > MAX_RECORDS_PER_ZONE:
        return False
    try:
        check_zone_records(zone, records)
    except ValueError:
        return False
    return True


def reconstruct_snapshot(request: IxfrReconstructRequest) -> dict[str, Any]:
    """Explore every valid fragment chain and classify the outcome."""
    target = request.target_serial
    start = (
        request.backup_serial,
        frozenset(_key(r) for r in request.backup_records),
    )

    # Fragments overshooting the target can never be part of a chain to it
    # (serials only move forward), so they are excluded up front.
    deltas_by_from: dict[int, list[IxfrDelta]] = {}
    for delta in request.deltas:
        if delta.to_serial <= target:
            deltas_by_from.setdefault(delta.from_serial, []).append(delta)

    valid_cache: dict[frozenset[RecKey], bool] = {}

    def state_valid(records: frozenset[RecKey]) -> bool:
        if records not in valid_cache:
            valid_cache[records] = _zone_state_valid(request.zone, records)
        return valid_cache[records]

    # Memoised DAG walk: state -> fragment ids of one valid chain reaching it.
    witness: dict[tuple[int, frozenset[RecKey]], tuple[DeltaId, ...]] = {start: ()}
    at_target: dict[frozenset[RecKey], tuple[DeltaId, ...]] = {}
    reachable_serials: set[int] = {request.backup_serial}
    stack = [start]
    while stack:
        serial, records = stack.pop()
        path = witness[(serial, records)]
        if serial == target:
            at_target.setdefault(records, path)
            continue
        for delta in deltas_by_from.get(serial, ()):
            nxt = _apply_delta(delta, records)
            if nxt is None or not state_valid(nxt):
                continue
            reachable_serials.add(delta.to_serial)
            state = (delta.to_serial, nxt)
            if state not in witness:
                witness[state] = path + (delta.id,)
                stack.append(state)

    base: dict[str, Any] = {
        "zone": request.zone,
        "backup_serial": request.backup_serial,
        "target_serial": target,
        "delta_count": len(request.deltas),
        "assumptions": ASSUMPTIONS,
    }

    if len(at_target) == 1:
        record_set, path = next(iter(at_target.items()))
        records = [_record_json(k) for k in sorted(record_set)]
        return {
            **base,
            "status": "unique_snapshot",
            "status_meaning": STATUS_MEANINGS["unique_snapshot"],
            "message": (
                f"所有到达序列号 {target} 的有效链得到同一组记录"
                f"（共 {len(records)} 条，忽略顺序）。records 已按 /plan 的"
                "规则校验，可直接填入 /plan 作为该区的 records / new_records。"
            ),
            "records": records,
            "record_count": len(records),
            "example_chain": list(path),
        }

    if at_target:
        # Deterministic choice of two witnesses: order the distinct result
        # sets and take the first two.
        ordered = sorted(at_target.items(), key=lambda item: sorted(item[0]))
        (set_a, path_a), (set_b, path_b) = ordered[0], ordered[1]
        diff_key = sorted(set_a.symmetric_difference(set_b))[0]
        return {
            **base,
            "status": "ambiguous",
            "status_meaning": STATUS_MEANINGS["ambiguous"],
            "message": (
                f"有 {len(at_target)} 组不同的记录可经有效链到达序列号 "
                f"{target}，无法确定唯一快照。以下两条有效链的结果不一致，"
                "并给出一处记录差异；请先核对各副本的 IXFR 日志，"
                "不要据此恢复或发布快照。"
            ),
            "chain_a": list(path_a),
            "chain_b": list(path_b),
            "difference": {
                "record": _record_json(diff_key),
                "present_in_chain_a": diff_key in set_a,
                "present_in_chain_b": diff_key in set_b,
            },
            "distinct_snapshots_at_target": len(at_target),
        }

    highest = max(s for s in reachable_serials if s < target)
    return {
        **base,
        "status": "unreachable",
        "status_meaning": STATUS_MEANINGS["unreachable"],
        "message": (
            f"没有任何有效链到达目标序列号 {target}；目标前最高可达序列号"
            f"为 {highest}。需要补齐缺失的差量片段后才能恢复快照。"
        ),
        "highest_reachable_serial": highest,
        "reachable_serials": sorted(s for s in reachable_serials if s < target),
    }
