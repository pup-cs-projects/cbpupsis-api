"""Explicit #99 integration seam. No verdict is safer than invented eligibility.

Replace this operation with the real shared #99 verdict once integrated. It must
use the supplied transaction, check every selected section, and never commit.
True means all prerequisite/gate/PE/waiver checks passed; False means violated.
"""

from cbpupsis_shared.domains.enrollment.exceptions import CertificateUnavailable


async def certificate_eligible(db, *, student_id, term_id, section_ids) -> bool:
    raise CertificateUnavailable
