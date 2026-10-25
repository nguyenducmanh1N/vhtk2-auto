"""Exception hierarchy for the device-control layer.

``VhtkError`` is the common root so the orchestrator can catch everything
from this tool in one place while letting genuine bugs (TypeError...)
propagate.
"""

from __future__ import annotations


class VhtkError(Exception):
    """Root of all tool-specific errors."""


class ADBError(VhtkError):
    """An ADB command failed or returned unusable output."""


class DeviceLost(ADBError):
    """ADB connection could not be re-established after all retries."""


class LDConsoleError(VhtkError):
    """ldconsole.exe failed, timed out, or produced unparsable output."""


class RateLimited(VhtkError):
    """Action rate exceeded the configured limit beyond the grace period.

    Raised when the input gate would have to wait longer than its grace
    period for a free slot — normal pacing never waits that long, so this
    almost always means a bug is spamming actions.
    """


class KillSwitchTriggered(VhtkError):
    """The kill-switch file appeared; every task must stop cleanly.

    Deliberately distinct from other errors: the state machine must treat
    it as "human ordered stop", not as a failure to recover from.
    """


class TaskError(VhtkError):
    """Base for in-task failures the state machine routes to RECOVER.

    Task code raises these when the screen does not behave as expected;
    they mean "this attempt failed", not "the tool is broken".
    """


class StateTimeout(TaskError):
    """An expected UI element did not appear within the state's time budget."""


class VerifyFailed(TaskError):
    """An action was performed but its expected effect was not observed."""


class TaskDefinitionError(VhtkError):
    """A task's state graph is invalid (programming error, not a runtime one).

    Raised at task construction or on an undeclared transition; must never
    be swallowed by recovery — it means the code itself is wrong.
    """


class SafetyLimitExceeded(VhtkError):
    """More irreversible actions were attempted than the session allows.

    Almost always a looping bug (e.g. a sell loop that never terminates);
    the task must ABORT, not recover and retry.
    """
