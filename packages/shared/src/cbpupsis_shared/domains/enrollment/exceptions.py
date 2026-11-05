"""Stable academic enrollment and waiver refusals."""

from cbpupsis_core.exceptions import ConflictError, ValidationError


class AcademicRefusalError(ConflictError):
    """A structured refusal from the shared academic evaluator."""


class AcademicDataUnavailableError(ConflictError):
    code = "ENROLL_ACADEMIC_DATA_UNAVAILABLE"

    def __init__(self):
        super().__init__("Reviewed academic metadata is unavailable.")


class WaiverEligibilityError(ConflictError):
    code = "ENROLL_PREREQUISITE_NOT_MET"

    def __init__(self):
        super().__init__("No qualifying unmet prerequisite and waiver condition.")


class WaiverPendingError(ConflictError):
    code = "ENROLL_WAIVER_PENDING"

    def __init__(self):
        super().__init__("A live waiver already exists.")


class WaiverStepMismatchError(ConflictError):
    code = "ENROLL_WAIVER_STEP_MISMATCH"

    def __init__(self):
        super().__init__("Reviewer is not assigned to this step.")


class WaiverAlreadyDecidedError(ConflictError):
    code = "REQUEST_ALREADY_DECIDED"

    def __init__(self):
        super().__init__("This waiver is already decided.")


class DecisionRemarksRequiredError(ValidationError):
    code = "DECISION_REMARKS_REQUIRED"

    def __init__(self):
        super().__init__("Rejection remarks are required.")


class VpaaReferenceRequiredError(ValidationError):
    code = "VALIDATION_ERROR"

    def __init__(self):
        super().__init__("The external VPAA approval reference is required.")
