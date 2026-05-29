"""Fix-as-PR pipeline.

Takes a ``FailureReport`` and opens a real pull request against the
agent's code with the fix applied. Each Fixer implementation is
host-specific (GitHub today; GitLab/Bitbucket later).
"""

from plivo_mirror.fixers.base import ApplyResult, Fixer, FixerError
from plivo_mirror.fixers.github import GitHubFixer

__all__ = ["ApplyResult", "Fixer", "FixerError", "GitHubFixer"]
