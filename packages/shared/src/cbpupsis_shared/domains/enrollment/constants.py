"""Academic enrollment permissions and transition vocabulary."""

CREATE_WAIVER_REQUEST = "CreateWaiverRequest"
READ_WAIVER_REQUEST = "ReadWaiverRequest"
APPROVE_WAIVER_REQUEST = "ApproveWaiverRequest"

PENDING_STATES = ("PENDING_CHAIR", "PENDING_DEAN", "PENDING_VPAA")
REVIEW_STEPS = {
    "chairperson": "PENDING_CHAIR",
    "dean": "PENDING_DEAN",
    "registrar": "PENDING_VPAA",
}
