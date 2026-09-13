"""The one exception the admin HTTP surface raises on bad input.

A leaf, extracted so `_parsing` can import it without reaching back into the package
`__init__` -- `from memotron.admin_server.__init__ import ...` is a circular
ImportError, and it is a mistake two of this refactor tools have now made.

It stays re-exported from the package: tests/test_review_regressions.py:1260 does
`pytest.raises(admin_server.HttpApiError)`."""

from __future__ import annotations


class HttpApiError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
