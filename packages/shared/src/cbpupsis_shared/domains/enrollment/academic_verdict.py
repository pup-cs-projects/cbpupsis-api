"""The certificate's academic verdict, read from the shared evaluator.

Uses the supplied transaction, checks every registered section, and never commits.
True means all prerequisite, gate, PE, and waiver checks passed; False means one is
violated. Missing academic data is CertificateUnavailable, never invented eligibility.
"""

from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.enrollment import academic
from cbpupsis_shared.domains.enrollment.exceptions import (
    AcademicDataUnavailableError,
    CertificateUnavailable,
)


async def certificate_eligible(db, *, student_id, term_id, section_ids) -> bool:
    term = await academics.get_academic_term(db, term_id)
    if term is None:
        raise CertificateUnavailable
    try:
        verdict = await academic.evaluate_registered(
            db, student_id=student_id, term=term, section_ids=section_ids
        )
    except (AcademicDataUnavailableError, ResourceNotFoundError):
        raise CertificateUnavailable from None
    return not verdict.blockers
