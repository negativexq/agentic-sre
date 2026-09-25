"""Canonical membership digest semantics for the base evidence manifest."""

from __future__ import annotations

from hashlib import sha256

from packages.rca.manifest import ManifestEntry, manifest_membership_digest


def test_manifest_membership_digest_uses_exact_sorted_pairs() -> None:
    membership = (
        ("OBJECT_VERSION", "10"),
        ("EVENT_VERSION", "20"),
        ("ALERT", "30"),
    )

    assert (
        manifest_membership_digest(membership)
        == sha256(b"ALERT:30\nEVENT_VERSION:20\nOBJECT_VERSION:10").hexdigest()
    )


def test_manifest_membership_digest_ignores_entry_order_and_sequence_order() -> None:
    first_sequence_order = (
        ("OBJECT_VERSION", "10"),
        ("EVENT_VERSION", "20"),
        ("ALERT", "30"),
    )
    different_sequence_order = (
        ("ALERT", "30"),
        ("OBJECT_VERSION", "10"),
        ("EVENT_VERSION", "20"),
    )

    assert manifest_membership_digest(first_sequence_order) == manifest_membership_digest(
        different_sequence_order
    )


def test_manifest_membership_digest_ignores_frozen_payload() -> None:
    first = ManifestEntry("ALERT", "30", {"status": "FIRING", "labels": {"team": "a"}})
    second = ManifestEntry("ALERT", "30", {"status": "RESOLVED", "labels": {"team": "b"}})

    def digest(entries: tuple[ManifestEntry, ...]) -> str:
        return manifest_membership_digest((entry.source_type, entry.source_id) for entry in entries)

    assert digest((first,)) == digest((second,))


def test_manifest_membership_change_changes_digest() -> None:
    original = (("EVENT_VERSION", "10"), ("OBJECT_VERSION", "20"))
    changed = (("EVENT_VERSION", "11"), ("OBJECT_VERSION", "20"))

    assert manifest_membership_digest(original) != manifest_membership_digest(changed)
