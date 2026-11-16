"""Unit (feature 028): field-aware redaction of RouterOS API rows (FR-006).

Mirrors the feature-026 redact_export guarantee for structured data: credential-named fields and
any known config secret are scrubbed to the stable placeholder; ordinary operational fields pass
through untouched.
"""

from __future__ import annotations

from obi.core.configstore import redact_records, redact_scalar

_REDACTED = "<redacted-by-obi>"


def test_credential_named_fields_are_redacted() -> None:
    rows = redact_records(
        [
            {
                "name": "ppp-user",
                "password": "hunter2xyz",
                "wpa2-pre-shared-key": "supersecretkey",
                "authentication-key": "authsecretval",
                "private-key": "PRIVKEYMATERIAL",
                "comment": "office link",
            }
        ]
    )
    row = rows[0]
    assert row["password"] == _REDACTED
    assert row["wpa2-pre-shared-key"] == _REDACTED
    assert row["authentication-key"] == _REDACTED
    assert row["private-key"] == _REDACTED
    # non-secret fields survive
    assert row["comment"] == "office link"
    assert row["name"] == "ppp-user"  # 'name' is NOT a secret outside /snmp


def test_snmp_name_and_community_redacted_only_under_snmp() -> None:
    under_snmp = redact_records([{"name": "public-community"}], path="snmp/community")
    assert under_snmp[0]["name"] == _REDACTED

    off_snmp = redact_records([{"name": "ether1"}], path="interface")
    assert off_snmp[0]["name"] == "ether1"

    trap = redact_records([{"trap-community": "s3cr3t", "enabled": "yes"}], path="/snmp")
    assert trap[0]["trap-community"] == _REDACTED
    assert trap[0]["enabled"] == "yes"


def test_value_backstop_redacts_known_secret_anywhere() -> None:
    rows = redact_records(
        [{"comment": "configured with fakesecret44028f", "address": "172.25.110.1"}],
        extra_secrets=("fakesecret44028f",),
    )
    assert "fakesecret44028f" not in rows[0]["comment"]
    assert _REDACTED in rows[0]["comment"]
    assert rows[0]["address"] == "172.25.110.1"


def test_non_string_values_pass_through() -> None:
    rows = redact_records([{"running": True, "mtu": 1500, "rx-byte": 42}])
    assert rows[0] == {"running": True, "mtu": 1500, "rx-byte": 42}


def test_inputs_not_mutated() -> None:
    original = [{"password": "abcd1234"}]
    redact_records(original)
    assert original[0]["password"] == "abcd1234"


def test_redact_scalar_only_backstops_known_secrets() -> None:
    assert redact_scalar("plain text") == "plain text"
    assert redact_scalar("has SEKRETVAL inside", ("SEKRETVAL",)) == f"has {_REDACTED} inside"
    # too-short literals are never redacted (would shred unrelated text)
    assert redact_scalar("a b c", ("ab",)) == "a b c"
