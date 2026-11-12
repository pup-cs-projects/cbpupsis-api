import pytest

from cbpupsis_shared.domains.curriculum.classification import classify_course_code


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NSTP 1", (True, None)),
        ("NSTP-002", (True, None)),
        ("CWTS 2", (True, None)),
        ("PATHFIT 1", (False, 1)),
        ("PE 4", (False, 4)),
        ("PHED 103", (False, 3)),
        ("pathfit 002", (False, 2)),
        ("PE 5", (False, None)),
        ("COMP 001", (False, None)),
        ("PEACE 1", (False, None)),
    ],
)
def test_course_code_classification(code, expected):
    assert classify_course_code(code) == expected
