"""Public calendar and term metadata interface."""

from cbpupsis_shared.domains.academics.service import (
    holidays,
    record_terms,
    resolve_term,
)

__all__ = ["holidays", "record_terms", "resolve_term"]
