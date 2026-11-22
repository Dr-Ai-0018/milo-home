"""Native execution profiles; the application never makes approval decisions."""

from enum import Enum


class NativeExecutionPolicy(Enum):
    LEGACY_DIRECT = "legacy_direct"
    AUTO_REVIEW = "auto_review"

    def resume_parameters(self):
        if self is NativeExecutionPolicy.AUTO_REVIEW:
            return {
                "sandbox": "workspace-write",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
            }
        return {
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
        }

    def matches(self, response):
        sandbox = response.get("sandbox", {})
        if not isinstance(sandbox, dict):
            return False
        if self is NativeExecutionPolicy.AUTO_REVIEW:
            return (
                sandbox.get("type") == "workspaceWrite"
                and response.get("approvalPolicy") == "on-request"
                and response.get("approvalsReviewer") == "auto_review"
            )
        return (
            sandbox.get("type") == "dangerFullAccess" and response.get("approvalPolicy") == "never"
        )
