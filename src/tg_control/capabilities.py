"""Capability discovery for the synthetic-only Telegram gateway.

Capability status is descriptive, not an authorization grant. The caller's
account/client/chat policy remains enforced by Gateway Core for every operation.
No record in this module implies a live Telegram connection.
"""

from __future__ import annotations

from typing import Any

from .adapters.base import Chat
from .features import FEATURE_SPECS, SYNTHETIC_FEATURE_OPERATIONS
from .policy import Permission, Scope

BASE_OPERATIONS = (
    ("list_chats", "messaging", "read", False, "TDLib chat-list methods; live mapping unverified"),
    (
        "search",
        "messaging",
        "read",
        False,
        "TDLib search methods; database/secret-chat limits apply",
    ),
    (
        "history",
        "messaging",
        "read",
        False,
        "TDLib getChatHistory; page-limited and not complete by default",
    ),
    ("get_message", "messaging", "read", False, "TDLib message lookup; cache/expiry limits apply"),
    (
        "get_media",
        "media",
        "read",
        False,
        "TDLib file download; protected/expired files are not bypassed",
    ),
    (
        "send",
        "messaging",
        "write",
        True,
        "TDLib sendMessage; asynchronous terminal outcome required",
    ),
    (
        "send_media",
        "media",
        "write",
        True,
        "TDLib message/media methods; size/rights/platform limits apply",
    ),
    (
        "edit",
        "messaging",
        "write",
        True,
        "TDLib editMessageText; ownership and edit-window limits apply",
    ),
    (
        "delete",
        "messaging",
        "write",
        True,
        "TDLib deleteMessages; rights and ownership vary by chat",
    ),
    (
        "forward",
        "messaging",
        "write",
        True,
        "TDLib forwardMessages; source Read and destination Write required",
    ),
    (
        "set_title",
        "groups",
        "admin",
        True,
        "TDLib chat-title mutation; connected-account rights required",
    ),
    (
        "set_member_status",
        "groups",
        "admin",
        True,
        "TDLib member-rights methods; exact admin rights required",
    ),
)

# All base operations either target a chat or return chat-scoped data. Feature
# actions declare this independently in FeatureSpec.chat_required.
CHAT_SCOPED_BASE_OPERATIONS = frozenset(row[0] for row in BASE_OPERATIONS)
ADDITIONAL_BASE_OPERATION_PERMISSIONS = {"forward": (Permission.READ,)}


def visible_operations_for_scopes(
    scopes: dict[Permission, Scope | None], chats: list[Chat]
) -> frozenset[str]:
    """Return operations available under at least one current permitted chat.

    Account-level operations need only their independent permission scope. Chat
    actions are visible only when that permission allows at least one adapter-
    reported chat. Unknown or missing chat metadata therefore hides chat actions.
    """
    visible: set[str] = set()
    for operation, _group, permission, _mutation, _official in BASE_OPERATIONS:
        required = tuple(
            dict.fromkeys(
                (Permission(permission), *ADDITIONAL_BASE_OPERATION_PERMISSIONS.get(operation, ()))
            )
        )
        if all(
            (scope := scopes.get(required_permission)) is not None
            and any(scope.allows(chat) for chat in chats)
            for required_permission in required
        ):
            visible.add(operation)
    for operation, spec in FEATURE_SPECS.items():
        scope = scopes.get(spec.permission)
        if scope is None:
            continue
        if not spec.chat_required or any(scope.allows(chat) for chat in chats):
            visible.add(operation)
    return frozenset(visible)


def _retry_safety(mutation: bool) -> str:
    return "only_definite_not_applied" if mutation else "safe_read_retry_with_reauthorization"


def capability_catalog(
    account_id: str,
    *,
    eligibility: dict[str, bool] | None = None,
    synthetic_operations: frozenset[str] = SYNTHETIC_FEATURE_OPERATIONS,
    visible_operations: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Return the live boundary and operation-level status for one authorized account."""
    eligibility = eligibility or {}
    rows: list[dict[str, Any]] = []
    for operation, group, permission, mutation, official in BASE_OPERATIONS:
        if visible_operations is not None and operation not in visible_operations:
            continue
        rows.append(
            {
                "operation": operation,
                "group": group,
                "permission": permission,
                "additional_permissions": [
                    required.value
                    for required in ADDITIONAL_BASE_OPERATION_PERMISSIONS.get(operation, ())
                ],
                "chat_required": operation in CHAT_SCOPED_BASE_OPERATIONS,
                "official_api_support": official,
                "adapter_support": "synthetic_fixture"
                if operation
                in {
                    "list_chats",
                    "search",
                    "history",
                    "get_message",
                    "get_media",
                    "send",
                    "send_media",
                    "edit",
                    "delete",
                    "forward",
                    "set_title",
                    "set_member_status",
                }
                else "not_implemented",
                "account_requirements": "personal account; platform controls chat visibility",
                "safe_retry": _retry_safety(mutation),
                "normalized_errors": [
                    "denied",
                    "not_found",
                    "unsupported_operation",
                    "timeout",
                    "outcome_unknown",
                ],
                "live_status": "blocked_compliance",
                "status": "synthetic_fixture_supported",
                "conditional_blockers": {},
            }
        )
    for operation, spec in FEATURE_SPECS.items():
        if visible_operations is not None and operation not in visible_operations:
            continue
        supported = operation in synthetic_operations
        dependency = spec.platform_dependency
        eligible = eligibility.get(dependency) if dependency else None
        if spec.blocked_reason:
            status = "approval_required"
        elif operation == "translate_message":
            status = "compliance_blocked"
        elif dependency and eligible is False:
            status = "capability_unavailable"
        elif dependency and eligible is None:
            status = "eligibility_unknown"
        elif supported:
            status = "synthetic_fixture_supported"
        else:
            status = "not_implemented_or_unverified"
        normalized_errors = _errors_for(dependency)
        if spec.blocked_reason:
            normalized_errors.append("approval_required")
        if operation == "translate_message":
            normalized_errors.append("compliance_blocked")
        for _, blocker in spec.conditional_blockers:
            if blocker not in normalized_errors:
                normalized_errors.append(blocker)
        rows.append(
            {
                "operation": operation,
                "group": spec.group,
                "permission": spec.permission.value,
                "additional_permissions": [],
                "chat_required": spec.chat_required,
                "official_api_support": _official_support(spec.group),
                "adapter_support": (
                    "synthetic_fixture" if supported else "not_implemented_or_version_unverified"
                ),
                "account_requirements": _account_requirements(spec.permission.value, dependency),
                "platform_dependency": dependency,
                "account_eligible": eligible,
                "blocker_reason": spec.blocked_reason,
                "conditional_blockers": dict(spec.conditional_blockers),
                "safe_retry": _retry_safety(spec.mutation),
                "normalized_errors": normalized_errors,
                "live_status": "blocked_compliance",
                "status": status,
            }
        )
    rows.sort(key=lambda row: (row["group"], row["operation"]))
    return {
        "account_id": account_id,
        "adapter_mode": "synthetic",
        "live_adapter_enabled": False,
        "live_blocker": "Compliance is unresolved; live Telegram access is blocked.",
        "capabilities": rows,
    }


def _official_support(group: str) -> str:
    by_group = {
        "messaging": "Official Telegram APIs/TDLib cover parts; verify each version mapping",
        "media": "TDLib media APIs exist; protections, expiry and size limits remain",
        "interactive": "Official APIs cover subsets; detect account eligibility at runtime",
        "groups": "TDLib admin APIs; connected-account rights are authoritative",
        "contacts": "TDLib contact APIs; privacy and flood limits apply",
        "account_settings": "TDLib account APIs; supported fields vary by release",
        "stories_advanced": "Story/statistics APIs vary by rights, privacy, Premium and release",
        "business_stars": "Business/Stars APIs vary by release and account eligibility",
    }
    return by_group.get(group, "Official API coverage not verified in the current offline boundary")


def _account_requirements(permission: str, dependency: str | None) -> str:
    requirements = ["personal user account"]
    if permission == "admin":
        requirements.append(
            "Gateway Admin scope and the connected account's actual Telegram rights"
        )
    if dependency:
        requirements.append(f"account/platform capability: {dependency}")
    return "; ".join(requirements)


def _errors_for(dependency: str | None) -> list[str]:
    errors = [
        "denied",
        "not_found",
        "unsupported_operation",
        "unsupported_platform",
        "permission_required",
    ]
    if dependency:
        errors.append("capability_unavailable")
    return errors
