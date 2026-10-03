import asyncio
import logging

import pytest

from salu.logging_setup import setup_logging
from salu.security.permissions import ApprovalBroker, ApprovalRequest, PermissionPolicy, Risk
from salu.security.redact import REDACTED, contains_secret, redact, redact_obj


# ---- permission policy ----------------------------------------------------
def test_low_auto_medium_confirm_high_confirm():
    p = PermissionPolicy(["low"])
    assert not p.evaluate("t", Risk.LOW).needs_approval
    assert p.evaluate("t", Risk.MEDIUM).needs_approval
    assert p.evaluate("t", Risk.HIGH).needs_approval


def test_high_can_never_be_auto_approved():
    p = PermissionPolicy(["low", "medium", "high"])
    assert Risk.HIGH not in p.auto_approve
    assert p.evaluate("t", Risk.HIGH).needs_approval
    assert not p.evaluate("t", Risk.MEDIUM).needs_approval


def test_overrides_only_raise_risk():
    p = PermissionPolicy(["low"], risk_overrides={"a": "high", "b": "low"})
    assert p.evaluate("a", Risk.LOW).risk == Risk.HIGH
    assert p.evaluate("b", Risk.HIGH).risk == Risk.HIGH  # cannot lower


def test_disabled_tool_blocked():
    d = PermissionPolicy(disabled_tools=["x"]).evaluate("x", Risk.LOW)
    assert not d.allowed


# ---- approval broker ------------------------------------------------------
async def test_no_approver_denies():
    assert await ApprovalBroker().request(ApprovalRequest("t", Risk.HIGH, "", "")) is False


async def test_approver_grants_and_denies():
    b = ApprovalBroker(timeout_seconds=2)
    answers = iter([True, False])

    async def ui(req):
        asyncio.get_running_loop().call_soon(b.resolve, req.id, next(answers))

    b.attach(ui)
    assert await b.request(ApprovalRequest("t", Risk.MEDIUM, "", "")) is True
    assert await b.request(ApprovalRequest("t", Risk.MEDIUM, "", "")) is False


async def test_timeout_denies():
    b = ApprovalBroker(timeout_seconds=0.1)

    async def ui(req):
        pass

    b.attach(ui)
    assert await b.request(ApprovalRequest("t", Risk.HIGH, "", "")) is False


async def test_deny_all_releases_pending():
    b = ApprovalBroker(timeout_seconds=5)

    async def ui(req):
        asyncio.get_running_loop().call_soon(b.deny_all)

    b.attach(ui)
    assert await b.request(ApprovalRequest("t", Risk.HIGH, "", "")) is False


# ---- redaction ------------------------------------------------------------
@pytest.mark.parametrize("secret", [
    "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAABCDEFGHIJKLMNOP",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
])
def test_redacts_known_secret_formats(secret):
    out = redact(f"value: {secret} end")
    assert secret not in out and REDACTED in out


def test_redacts_key_value_and_urls():
    assert redact("password=hunter2") == f"password={REDACTED}"
    assert redact("postgres://admin:s3cret@db:5432/x") == f"postgres://admin:{REDACTED}@db:5432/x"
    assert redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz") .endswith(REDACTED)


def test_redacts_env_secret_values(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "zzzz-very-private-value")
    assert "very-private" not in redact("token is zzzz-very-private-value")


def test_plain_text_untouched():
    text = "CPU 18%, RAM 52%, Docker running. The keyboard is fine."
    assert redact(text) == text and not contains_secret(text)


def test_redact_obj_secret_keys():
    out = redact_obj({"user": "salu", "api_key": "abc", "nested": [{"password": "x"}]})
    assert out == {"user": "salu", "api_key": REDACTED, "nested": [{"password": REDACTED}]}


def test_logs_are_redacted(tmp_path):
    log_file = tmp_path / "t.log"
    setup_logging("INFO", log_file, console=False)
    logging.getLogger("salu.test").info("connecting with token=%s", "sk-ant-oat01-SECRETSECRETSECRET")
    for h in logging.getLogger().handlers:
        h.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "SECRETSECRET" not in content and REDACTED in content
