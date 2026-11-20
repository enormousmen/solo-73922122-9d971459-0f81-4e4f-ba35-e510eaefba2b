"""Tests for /ixfr/reconstruct: chaining rules, outcomes, validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ixfr import IxfrReconstructRequest, reconstruct_snapshot
from app.main import app

client = TestClient(app)

ZONE = "ops.example.com"


def rec(name: str, rtype: str, value: str, ttl: int = 300) -> dict:
    return {"name": name, "type": rtype, "value": value, "ttl": ttl}


def a(name: str, value: str, ttl: int = 300) -> dict:
    return rec(name, "A", value, ttl)


def cname(name: str, target: str, ttl: int = 300) -> dict:
    return rec(name, "CNAME", target, ttl)


def delta(
    id: str | int,
    frm: int,
    to: int,
    deletions: list[dict] | None = None,
    additions: list[dict] | None = None,
) -> dict:
    return {
        "id": id,
        "from_serial": frm,
        "to_serial": to,
        "deletions": deletions or [],
        "additions": additions or [],
    }


def payload(
    backup_records: list[dict],
    target: int,
    deltas: list[dict],
    backup_serial: int = 100,
    zone: str = ZONE,
) -> dict:
    return {
        "zone": zone,
        "backup_serial": backup_serial,
        "backup_records": backup_records,
        "target_serial": target,
        "deltas": deltas,
    }


def reconstruct(body: dict) -> dict:
    return reconstruct_snapshot(IxfrReconstructRequest.model_validate(body))


def post(body: dict) -> dict:
    response = client.post("/ixfr/reconstruct", json=body)
    assert response.status_code == 200, response.text
    return response.json()


BACKUP = [
    a("www.ops.example.com", "10.0.0.1"),
    cname("api.ops.example.com", "www.ops.example.com", 60),
]

# The canonical three-fragment chain used by several tests, submitted out of
# order on purpose: 100 -> 101 -> 102 -> 103.
CHAIN_DELTAS = [
    delta(
        "frag-c",
        101,
        102,
        deletions=[a("www.ops.example.com", "10.0.0.1")],
        additions=[a("www.ops.example.com", "10.0.0.2")],
    ),
    delta("frag-a", 100, 101, additions=[a("db.ops.example.com", "10.0.1.1", 120)]),
    delta(
        "frag-b",
        102,
        103,
        deletions=[cname("api.ops.example.com", "www.ops.example.com", 60)],
        additions=[cname("api.ops.example.com", "db.ops.example.com", 60)],
    ),
]

EXPECTED_FINAL = [
    cname("api.ops.example.com", "db.ops.example.com", 60),
    a("db.ops.example.com", "10.0.1.1", 120),
    a("www.ops.example.com", "10.0.0.2"),
]


# --------------------------------------------------------------------------- #
# unique_snapshot
# --------------------------------------------------------------------------- #


def test_unique_snapshot_from_out_of_order_fragments() -> None:
    out = reconstruct(payload(BACKUP, 103, CHAIN_DELTAS))
    assert out["status"] == "unique_snapshot"
    # Fragments chain by serial, not by submission order.
    assert out["example_chain"] == ["frag-a", "frag-c", "frag-b"]
    assert out["records"] == EXPECTED_FINAL
    assert out["record_count"] == 3
    assert out["zone"] == ZONE
    assert out["backup_serial"] == 100
    assert out["target_serial"] == 103


def test_empty_chain_when_target_equals_backup() -> None:
    out = reconstruct(payload(BACKUP, 100, []))
    assert out["status"] == "unique_snapshot"
    assert out["example_chain"] == []
    assert out["records"] == sorted(BACKUP, key=lambda r: (r["name"], r["type"], r["value"], r["ttl"]))


def test_duplicate_fragments_from_replicas_stay_unique() -> None:
    """Two replicas logged the same 100->101 change under different ids."""
    deltas = [
        delta("replica-1", 100, 101, additions=[a("db.ops.example.com", "10.0.1.1")]),
        delta("replica-2", 100, 101, additions=[a("db.ops.example.com", "10.0.1.1")]),
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unique_snapshot"
    assert out["record_count"] == 3


def test_diamond_chains_reconverging_to_same_records_are_unique() -> None:
    """Different intermediate states, identical final record set."""
    deltas = [
        delta("add-x-first", 100, 101, additions=[a("x.ops.example.com", "10.0.1.1")]),
        delta("then-y", 101, 102, additions=[a("y.ops.example.com", "10.0.1.2")]),
        delta("add-y-first", 100, 101, additions=[a("y.ops.example.com", "10.0.1.2")]),
        delta("then-x", 101, 102, additions=[a("x.ops.example.com", "10.0.1.1")]),
    ]
    out = reconstruct(payload(BACKUP, 102, deltas))
    assert out["status"] == "unique_snapshot"
    assert out["record_count"] == 4


def test_unusable_fragment_does_not_break_the_chain() -> None:
    """A fragment whose deletion does not match is simply skipped."""
    deltas = CHAIN_DELTAS + [
        delta(
            "frag-x",
            100,
            101,
            deletions=[a("www.ops.example.com", "10.0.0.1", ttl=60)],  # wrong TTL
        )
    ]
    out = reconstruct(payload(BACKUP, 103, deltas))
    assert out["status"] == "unique_snapshot"
    assert out["example_chain"] == ["frag-a", "frag-c", "frag-b"]


def test_overshooting_fragment_is_ignored() -> None:
    deltas = CHAIN_DELTAS + [delta("jump", 100, 999)]
    out = reconstruct(payload(BACKUP, 103, deltas))
    assert out["status"] == "unique_snapshot"
    assert out["records"] == EXPECTED_FINAL


def test_delete_and_readd_same_record_in_one_delta_changes_ttl() -> None:
    deltas = [
        delta(
            "retTL",
            100,
            101,
            deletions=[a("www.ops.example.com", "10.0.0.1", ttl=300)],
            additions=[a("www.ops.example.com", "10.0.0.1", ttl=60)],
        )
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unique_snapshot"
    assert a("www.ops.example.com", "10.0.0.1", ttl=60) in out["records"]
    assert a("www.ops.example.com", "10.0.0.1", ttl=300) not in out["records"]


def test_snapshot_can_be_fed_into_plan() -> None:
    out = reconstruct(payload(BACKUP, 103, CHAIN_DELTAS))
    assert out["status"] == "unique_snapshot"
    plan_body = {
        "zones": [
            {"name": ZONE, "records": out["records"], "new_records": out["records"]}
        ],
        "required_domains": ["api.ops.example.com"],
    }
    response = client.post("/plan", json=plan_body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "safe_order_found"


# --------------------------------------------------------------------------- #
# ambiguous
# --------------------------------------------------------------------------- #


def test_conflicting_replica_fragments_are_ambiguous() -> None:
    deltas = [
        delta("replica-a", 100, 101, additions=[a("api2.ops.example.com", "10.0.2.1")]),
        delta("replica-b", 100, 101, additions=[a("api2.ops.example.com", "10.0.2.2")]),
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "ambiguous"
    assert "records" not in out
    assert out["chain_a"] == ["replica-a"]
    assert out["chain_b"] == ["replica-b"]
    diff = out["difference"]
    assert diff["record"] == a("api2.ops.example.com", "10.0.2.1")
    assert diff["present_in_chain_a"] is True
    assert diff["present_in_chain_b"] is False
    assert out["distinct_snapshots_at_target"] == 2


def test_ambiguity_from_a_deletion_difference() -> None:
    """One replica dropped www at 101, the other kept it."""
    deltas = [
        delta(
            "drop-www",
            100,
            101,
            deletions=[a("www.ops.example.com", "10.0.0.1")],
        ),
        delta("keep-www", 100, 101),
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "ambiguous"
    assert "records" not in out
    assert sorted([out["chain_a"], out["chain_b"]]) == [["drop-www"], ["keep-www"]]
    assert out["difference"]["record"] == a("www.ops.example.com", "10.0.0.1")


def test_ambiguity_deep_in_the_chain_reports_full_fragment_ids() -> None:
    shared = delta("shared", 100, 101, additions=[a("db.ops.example.com", "10.0.1.1")])
    deltas = [
        shared,
        delta("fork-1", 101, 102, additions=[a("x.ops.example.com", "10.0.2.1")]),
        delta("fork-2", 101, 102, additions=[a("x.ops.example.com", "10.0.2.2")]),
    ]
    out = reconstruct(payload(BACKUP, 102, deltas))
    assert out["status"] == "ambiguous"
    assert out["chain_a"] == ["shared", "fork-1"]
    assert out["chain_b"] == ["shared", "fork-2"]
    assert out["difference"]["record"] == a("x.ops.example.com", "10.0.2.1")


# --------------------------------------------------------------------------- #
# unreachable
# --------------------------------------------------------------------------- #


def test_missing_fragment_reports_highest_reachable_serial() -> None:
    deltas = [
        delta("first", 100, 101, additions=[a("a.ops.example.com", "10.0.2.1")]),
        # 101 -> 102 is missing.
        delta("last", 102, 103, additions=[a("b.ops.example.com", "10.0.2.2")]),
    ]
    out = reconstruct(payload(BACKUP, 103, deltas))
    assert out["status"] == "unreachable"
    assert "records" not in out
    assert out["highest_reachable_serial"] == 101
    assert out["reachable_serials"] == [100, 101]


def test_no_applicable_fragment_at_all() -> None:
    deltas = [delta("elsewhere", 500, 501)]
    out = reconstruct(payload(BACKUP, 103, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100
    assert out["reachable_serials"] == [100]


def test_overshooting_fragment_cannot_reach_target() -> None:
    deltas = [delta("jump", 100, 200)]
    out = reconstruct(payload(BACKUP, 150, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100


def test_deletion_must_match_the_full_record_including_ttl() -> None:
    """The fragment deletes www with the wrong TTL, so it never applies."""
    deltas = [
        delta(
            "wrong-ttl",
            100,
            101,
            deletions=[a("www.ops.example.com", "10.0.0.1", ttl=60)],
            additions=[a("www.ops.example.com", "10.0.0.9")],
        )
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100


def test_deletion_of_a_nonexistent_record_does_not_apply() -> None:
    deltas = [
        delta("ghost", 100, 101, deletions=[a("ghost.ops.example.com", "10.9.9.9")])
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100


def test_addition_must_not_duplicate_a_surviving_record() -> None:
    deltas = [
        delta(
            "dup",
            100,
            101,
            additions=[a("www.ops.example.com", "10.0.0.1")],  # already present
        )
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100


def test_addition_may_repeat_a_record_the_same_delta_deletes() -> None:
    """Delete-then-readd inside one fragment is a well-defined no-op."""
    deltas = [
        delta(
            "noop",
            100,
            101,
            deletions=[a("www.ops.example.com", "10.0.0.1")],
            additions=[a("www.ops.example.com", "10.0.0.1")],
        )
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unique_snapshot"
    assert a("www.ops.example.com", "10.0.0.1") in out["records"]


# --------------------------------------------------------------------------- #
# Post-delta /plan validation of the zone state
# --------------------------------------------------------------------------- #


def test_fragment_creating_a_and_cname_at_one_name_is_rejected() -> None:
    deltas = [
        delta(
            "bad",
            100,
            101,
            additions=[cname("www.ops.example.com", "db.ops.example.com")],
        )
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100


def test_fragment_creating_apex_cname_is_rejected() -> None:
    deltas = [
        delta("bad", 100, 101, additions=[cname("ops.example.com", "www.ops.example.com")])
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unreachable"


def test_fragment_with_out_of_zone_record_is_rejected() -> None:
    deltas = [
        delta("bad", 100, 101, additions=[a("www.other.example.com", "10.0.0.1")])
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unreachable"


def test_fragment_creating_name_below_cname_is_rejected() -> None:
    deltas = [
        delta(
            "bad",
            100,
            101,
            additions=[a("deep.api.ops.example.com", "10.0.3.1")],
        )
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unreachable"


def test_invalid_branch_pruned_but_valid_branch_still_unique() -> None:
    """One replica's fragment yields an illegal zone; the other's does not."""
    deltas = [
        delta(
            "bad",
            100,
            101,
            additions=[cname("www.ops.example.com", "db.ops.example.com")],
        ),
        delta("good", 100, 101, additions=[a("db.ops.example.com", "10.0.1.1")]),
    ]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unique_snapshot"
    assert out["example_chain"] == ["good"]


def test_state_breaking_record_limit_is_rejected() -> None:
    """/plan accepts at most 256 records per zone, so a state beyond that is
    not a valid link in the chain."""
    backup = [a(f"h{i}.ops.example.com", f"10.0.{i // 256}.{i % 256}") for i in range(256)]
    deltas = [delta("overflow", 100, 101, additions=[a("extra.ops.example.com", "10.1.0.1")])]
    out = reconstruct(payload(backup, 101, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100


def test_mid_chain_invalid_state_makes_later_fragments_unusable() -> None:
    deltas = [
        delta(
            "bad",
            100,
            101,
            additions=[cname("www.ops.example.com", "db.ops.example.com")],
        ),
        delta("after", 101, 102, additions=[a("db.ops.example.com", "10.0.1.1")]),
    ]
    out = reconstruct(payload(BACKUP, 102, deltas))
    assert out["status"] == "unreachable"
    assert out["highest_reachable_serial"] == 100


# --------------------------------------------------------------------------- #
# Request validation (422)
# --------------------------------------------------------------------------- #


def test_rejects_target_earlier_than_backup() -> None:
    with pytest.raises(ValidationError, match="must not be earlier"):
        IxfrReconstructRequest.model_validate(payload(BACKUP, 99, []))


def test_rejects_delta_that_does_not_move_forward() -> None:
    with pytest.raises(ValidationError, match="must move forward"):
        IxfrReconstructRequest.model_validate(
            payload(BACKUP, 101, [delta("flat", 100, 100)])
        )
    with pytest.raises(ValidationError, match="must move forward"):
        IxfrReconstructRequest.model_validate(
            payload(BACKUP, 101, [delta("backwards", 101, 100)])
        )


def test_rejects_duplicate_delta_ids() -> None:
    deltas = [delta("same", 100, 101), delta("same", 101, 102)]
    with pytest.raises(ValidationError, match="unique"):
        IxfrReconstructRequest.model_validate(payload(BACKUP, 102, deltas))


def test_rejects_more_than_twenty_deltas() -> None:
    deltas = [delta(i, 1000 + i, 1001 + i) for i in range(21)]
    with pytest.raises(ValidationError):
        IxfrReconstructRequest.model_validate(payload(BACKUP, 2000, deltas))


def test_accepts_exactly_twenty_deltas() -> None:
    deltas = [delta(i, 100 + i, 101 + i) for i in range(20)]
    out = reconstruct(payload(BACKUP, 120, deltas))
    assert out["status"] == "unique_snapshot"
    assert out["example_chain"] == list(range(20))


def test_rejects_serial_out_of_uint32_range() -> None:
    with pytest.raises(ValidationError):
        IxfrReconstructRequest.model_validate(payload(BACKUP, 2**32, []))
    with pytest.raises(ValidationError):
        IxfrReconstructRequest.model_validate(payload(BACKUP, 100, [], backup_serial=-1))


def test_rejects_invalid_backup_zone_records() -> None:
    bad = BACKUP + [cname("www.ops.example.com", "db.ops.example.com")]
    with pytest.raises(ValidationError, match="both A and CNAME"):
        IxfrReconstructRequest.model_validate(payload(bad, 100, []))


def test_rejects_backup_record_outside_zone() -> None:
    bad = BACKUP + [a("www.other.example.com", "10.0.0.1")]
    with pytest.raises(ValidationError, match="not owned by zone"):
        IxfrReconstructRequest.model_validate(payload(bad, 100, []))


def test_rejects_invalid_record_value() -> None:
    bad = [rec("www.ops.example.com", "A", "not-an-ip")]
    with pytest.raises(ValidationError, match="IPv4"):
        IxfrReconstructRequest.model_validate(payload(bad, 100, []))


def test_rejects_empty_delta_id() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        IxfrReconstructRequest.model_validate(
            payload(BACKUP, 101, [delta("  ", 100, 101)])
        )


def test_integer_delta_ids_are_echoed_back() -> None:
    deltas = [delta(7, 100, 101, additions=[a("x.ops.example.com", "10.0.1.1")])]
    out = reconstruct(payload(BACKUP, 101, deltas))
    assert out["status"] == "unique_snapshot"
    assert out["example_chain"] == [7]


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def test_endpoint_round_trip_unique() -> None:
    body = post(payload(BACKUP, 103, CHAIN_DELTAS))
    assert body["status"] == "unique_snapshot"
    assert body["records"] == EXPECTED_FINAL


def test_endpoint_ambiguous_and_unreachable_return_no_snapshot() -> None:
    ambiguous = post(
        payload(
            BACKUP,
            101,
            [
                delta("p", 100, 101, additions=[a("x.ops.example.com", "10.0.2.1")]),
                delta("q", 100, 101, additions=[a("x.ops.example.com", "10.0.2.2")]),
            ],
        )
    )
    assert ambiguous["status"] == "ambiguous"
    assert "records" not in ambiguous

    unreachable = post(payload(BACKUP, 105, []))
    assert unreachable["status"] == "unreachable"
    assert "records" not in unreachable
    assert unreachable["highest_reachable_serial"] == 100


def test_endpoint_validation_error_is_422() -> None:
    response = client.post("/ixfr/reconstruct", json=payload(BACKUP, 99, []))
    assert response.status_code == 422
    response = client.post(
        "/ixfr/reconstruct",
        json=payload(BACKUP, 101, [delta("x", 100, 100)]),
    )
    assert response.status_code == 422


def test_openapi_documents_endpoint() -> None:
    spec = client.get("/openapi.json").json()
    assert "/ixfr/reconstruct" in spec["paths"]
    response_schema = spec["components"]["schemas"]["IxfrReconstructResponse"]
    assert response_schema["properties"]["status"]["enum"] == [
        "unique_snapshot",
        "ambiguous",
        "unreachable",
    ]


# --------------------------------------------------------------------------- #
# Randomised equivalence with an independent brute-force chain enumerator
# --------------------------------------------------------------------------- #


def _brute_force(
    backup_records: list[dict], backup_serial: int, deltas: list[dict]
) -> tuple[dict[int, set[frozenset]], set[int]]:
    """Independently enumerate every valid chain.

    Returns ``{serial: {record sets reachable at that serial}}`` plus the set
    of all reachable serials.  Records are ``(name, type, value, ttl)`` tuples.
    """
    from app.models import check_zone_records

    def key(r: dict) -> tuple:
        return (r["name"].rstrip(".").lower(), r["type"].upper(), r["value"], r["ttl"])

    def state_valid(records: frozenset) -> bool:
        if len(records) > 256:
            return False

        class _R:
            def __init__(self, k: tuple) -> None:
                self.name, self.type, self.value, _ = k

        try:
            check_zone_records(ZONE, [_R(k) for k in records])
        except ValueError:
            return False
        return True

    start = frozenset(key(r) for r in backup_records)
    at_serial: dict[int, set[frozenset]] = {}
    reachable = {backup_serial}

    def walk(serial: int, records: frozenset, used: frozenset) -> None:
        at_serial.setdefault(serial, set()).add(records)
        for i, d in enumerate(deltas):
            if i in used or d["from_serial"] != serial:
                continue
            deleted = {key(r) for r in d["deletions"]}
            if not deleted <= records:
                continue
            survivors = records - deleted
            added = {key(r) for r in d["additions"]}
            if added & survivors:
                continue
            nxt = survivors | added
            if not state_valid(nxt):
                continue
            reachable.add(d["to_serial"])
            walk(d["to_serial"], nxt, used | {i})

    walk(backup_serial, start, frozenset())
    return at_serial, reachable


def test_matches_brute_force_on_random_instances() -> None:
    import random

    rng = random.Random(20261120)
    names = [f"n{i}.{ZONE}" for i in range(4)]
    compared = 0
    for _ in range(400):
        backup = [a(names[0], "10.0.0.1")]
        serials = sorted(rng.sample(range(100, 130), rng.randint(2, 6)))
        target = serials[-1]
        deltas = []
        for n in range(rng.randint(0, 8)):
            frm = rng.choice(serials[:-1])
            to = rng.choice([s for s in serials if s > frm])
            touched = rng.choice(names)
            ip = f"10.0.{rng.randint(0, 2)}.{rng.randint(1, 3)}"
            ttl = rng.choice([60, 300])
            deletions = [a(touched, ip, ttl)] if rng.random() < 0.4 else []
            additions = [a(touched, ip, ttl)] if rng.random() < 0.5 else []
            deltas.append(delta(n, frm, to, deletions=deletions, additions=additions))

        body = payload(backup, target, deltas)
        out = reconstruct(body)
        at_serial, reachable = _brute_force(backup, 100, deltas)
        expected_sets = at_serial.get(target, set())

        if len(expected_sets) == 1:
            assert out["status"] == "unique_snapshot", body
            got = {
                (r["name"], r["type"], r["value"], r["ttl"]) for r in out["records"]
            }
            assert got == set(next(iter(expected_sets)))
        elif expected_sets:
            assert out["status"] == "ambiguous", body
            assert "records" not in out
            # The two reported chains really do reach the target with
            # different record sets, and the difference record is real.
            assert out["chain_a"] != out["chain_b"]
            difference = out["difference"]
            assert difference["present_in_chain_a"] != difference["present_in_chain_b"]
            diff = difference["record"]
            key = (diff["name"], diff["type"], diff["value"], diff["ttl"])
            membership = [key in s for s in expected_sets]
            assert any(membership) and not all(membership)
        else:
            assert out["status"] == "unreachable", body
            assert "records" not in out
            highest = max(s for s in reachable if s < target)
            assert out["highest_reachable_serial"] == highest
        compared += 1
    assert compared == 400
