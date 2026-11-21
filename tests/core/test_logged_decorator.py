"""Tests for the ``@logged`` service decorator.

Covers both ``async def`` and ``def`` functions across success, expected
``AppError`` refusals, unexpected exceptions, slow-call warnings, request id
correlation, argument/return-value privacy, import-time field validation,
and wrapper signature preservation.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cbpupsis_core import logged
from cbpupsis_core.config import settings
from cbpupsis_core.exceptions import ConflictError, NotFoundError
from cbpupsis_core.logging import CorrelationFilter, JsonFormatter, request_id_var
from cbpupsis_core.middleware import register_middleware


def _event_records(
    caplog: pytest.LogCaptureFixture, event: str
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == event]


def _end_records(
    caplog: pytest.LogCaptureFixture, event: str
) -> list[logging.LogRecord]:
    return [r for r in _event_records(caplog, event) if r.phase == "end"]


class TestOutcomes:
    async def test_async_ok_writes_start_and_end_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @logged("grades.finalize")
        async def finalize_grades(section_id: str) -> dict[str, str]:
            return {"status": "done", "secret": "return-value-should-not-log"}

        with caplog.at_level(logging.DEBUG):
            result = await finalize_grades("sec-1")

        assert result == {"status": "done", "secret": "return-value-should-not-log"}
        records = _event_records(caplog, "grades.finalize")
        assert len(records) == 2

        start, end = records
        assert start.levelno == logging.DEBUG
        assert start.phase == "start"

        assert end.levelno == logging.INFO
        assert end.phase == "end"
        assert end.outcome == "ok"
        assert isinstance(end.duration_ms, int | float)
        assert end.duration_ms >= 0

    def test_sync_ok_writes_start_and_end_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @logged("grades.calculate")
        def calculate(section_id: str) -> int:
            return 42

        with caplog.at_level(logging.DEBUG):
            result = calculate("sec-2")

        assert result == 42
        records = _event_records(caplog, "grades.calculate")
        assert len(records) == 2

        start, end = records
        assert start.levelno == logging.DEBUG
        assert start.phase == "start"

        assert end.levelno == logging.INFO
        assert end.phase == "end"
        assert end.outcome == "ok"
        assert isinstance(end.duration_ms, int | float)
        assert end.duration_ms >= 0

    async def test_async_app_error_logs_refused_and_reraises_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = ConflictError("Already finalized", code="GRADES_ALREADY_FINALIZED")

        @logged("grades.finalize_refused")
        async def finalize() -> None:
            raise original

        with caplog.at_level(logging.DEBUG), pytest.raises(ConflictError) as exc_info:
            await finalize()

        assert exc_info.value is original
        end_records = _end_records(caplog, "grades.finalize_refused")
        assert len(end_records) == 1
        end = end_records[0]
        assert end.levelno == logging.INFO
        assert end.outcome == "refused"
        assert end.error_code == "GRADES_ALREADY_FINALIZED"
        assert isinstance(end.duration_ms, int | float)
        assert end.exc_info is None

    def test_sync_app_error_logs_refused_and_reraises_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = NotFoundError("Section missing")

        @logged("grades.lookup_refused")
        def lookup() -> None:
            raise original

        with caplog.at_level(logging.DEBUG), pytest.raises(NotFoundError) as exc_info:
            lookup()

        assert exc_info.value is original
        end_records = _end_records(caplog, "grades.lookup_refused")
        assert len(end_records) == 1
        end = end_records[0]
        assert end.levelno == logging.INFO
        assert end.outcome == "refused"
        assert end.error_code == "NotFoundError"
        assert isinstance(end.duration_ms, int | float)
        assert end.exc_info is None

    async def test_async_unexpected_error_logs_error_with_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        boom = RuntimeError("db socket dropped")

        @logged("grades.finalize_boom")
        async def finalize() -> None:
            raise boom

        with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as exc_info:
            await finalize()

        assert exc_info.value is boom
        end_records = _end_records(caplog, "grades.finalize_boom")
        assert len(end_records) == 1
        end = end_records[0]
        assert end.levelno == logging.ERROR
        assert end.outcome == "error"
        assert end.error_type == "RuntimeError"
        assert isinstance(end.duration_ms, int | float)
        assert end.exc_info is not None
        assert end.exc_info[1] is boom

    def test_sync_unexpected_error_logs_error_with_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        boom = ValueError("bad arithmetic")

        @logged("grades.compute_boom")
        def compute() -> None:
            raise boom

        with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as exc_info:
            compute()

        assert exc_info.value is boom
        end_records = _end_records(caplog, "grades.compute_boom")
        assert len(end_records) == 1
        end = end_records[0]
        assert end.levelno == logging.ERROR
        assert end.outcome == "error"
        assert end.error_type == "ValueError"
        assert isinstance(end.duration_ms, int | float)
        assert end.exc_info is not None
        assert end.exc_info[1] is boom


class TestSlowCalls:
    async def test_async_slow_call_logs_at_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticks = iter([10.0, 10.25])
        monkeypatch.setattr(time, "perf_counter", lambda: next(ticks))

        @logged("grades.slow_async", slow_ms=100)
        async def slow_op() -> str:
            return "ok"

        with caplog.at_level(logging.DEBUG):
            await slow_op()

        [end] = _end_records(caplog, "grades.slow_async")
        assert end.levelno == logging.WARNING
        assert end.outcome == "ok"
        assert end.duration_ms == 250.0

    def test_sync_slow_call_uses_settings_default_threshold(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "log_slow_call_ms", 50)
        ticks = iter([5.0, 5.12])
        monkeypatch.setattr(time, "perf_counter", lambda: next(ticks))

        @logged("grades.slow_sync")
        def slow_op() -> str:
            return "ok"

        with caplog.at_level(logging.DEBUG):
            slow_op()

        [end] = _end_records(caplog, "grades.slow_sync")
        assert end.levelno == logging.WARNING
        assert end.outcome == "ok"
        assert end.duration_ms == 120.0

    async def test_slow_unexpected_error_stays_at_error_level(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticks = iter([1.0, 5.0])
        monkeypatch.setattr(time, "perf_counter", lambda: next(ticks))

        @logged("grades.slow_error", slow_ms=10)
        async def fail_slowly() -> None:
            raise RuntimeError("timed out")

        with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
            await fail_slowly()

        [end] = _end_records(caplog, "grades.slow_error")
        assert end.levelno == logging.ERROR
        assert end.outcome == "error"


class TestCorrelationId:
    async def test_records_carry_request_id_from_middleware_async(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        test_app = FastAPI()
        register_middleware(test_app)

        @logged("grades.middleware_async")
        async def service_fn() -> dict[str, bool]:
            return {"ok": True}

        @test_app.get("/check")
        async def endpoint() -> dict[str, bool]:
            return await service_fn()

        corr_filter = CorrelationFilter()
        caplog.handler.addFilter(corr_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                async with AsyncClient(
                    transport=ASGITransport(app=test_app),
                    base_url="http://test",
                ) as http:
                    response = await http.get(
                        "/check", headers={"X-Request-ID": "req-async-42"}
                    )
        finally:
            caplog.handler.removeFilter(corr_filter)

        assert response.status_code == 200
        records = _event_records(caplog, "grades.middleware_async")
        assert len(records) == 2
        assert all(r.request_id == "req-async-42" for r in records)

    def test_records_carry_request_id_from_contextvar_sync(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @logged("grades.middleware_sync")
        def service_fn() -> str:
            return "ok"

        corr_filter = CorrelationFilter()
        caplog.handler.addFilter(corr_filter)
        token = request_id_var.set("req-sync-99")
        try:
            with caplog.at_level(logging.DEBUG):
                service_fn()
        finally:
            request_id_var.reset(token)
            caplog.handler.removeFilter(corr_filter)

        records = _event_records(caplog, "grades.middleware_sync")
        assert len(records) == 2
        assert all(r.request_id == "req-sync-99" for r in records)


class TestPrivacyAndFields:
    async def test_async_omits_arguments_and_return_values_by_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @logged("grades.private_async")
        async def submit(
            section_id: str, student_number: str, grade: str
        ) -> dict[str, str]:
            return {"returned": "secret-return-payload"}

        with caplog.at_level(logging.DEBUG):
            await submit("sec-777", "2024-00001-MN-0", "1.25")

        records = _event_records(caplog, "grades.private_async")
        rendered = [JsonFormatter().format(r) for r in records]
        joined = "\n".join(rendered)
        assert "sec-777" not in joined
        assert "2024-00001-MN-0" not in joined
        assert "1.25" not in joined
        assert "secret-return-payload" not in joined

    def test_sync_omits_arguments_and_return_values_by_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @logged("grades.private_sync")
        def compute(section_id: str, student_number: str, grade: str) -> str:
            return "secret-sync-return"

        with caplog.at_level(logging.DEBUG):
            compute("sec-888", "2024-00002-MN-0", "1.50")

        records = _event_records(caplog, "grades.private_sync")
        rendered = [JsonFormatter().format(r) for r in records]
        joined = "\n".join(rendered)
        assert "sec-888" not in joined
        assert "2024-00002-MN-0" not in joined
        assert "1.50" not in joined
        assert "secret-sync-return" not in joined

    async def test_async_includes_only_opted_in_id_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sec_id = uuid.uuid4()

        @logged("grades.opted_async", fields=("section_id",))
        async def finalize(*, section_id: uuid.UUID, student_number: str) -> str:
            return "hidden-return"

        with caplog.at_level(logging.DEBUG):
            await finalize(section_id=sec_id, student_number="2024-99999-MN-0")

        records = _event_records(caplog, "grades.opted_async")
        assert len(records) == 2
        for record in records:
            assert record.section_id == sec_id
            formatted = JsonFormatter().format(record)
            assert str(sec_id) in formatted
            assert "2024-99999-MN-0" not in formatted
            assert "hidden-return" not in formatted

    def test_sync_includes_only_opted_in_id_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sec_id = uuid.uuid4()

        @logged("grades.opted_sync", fields=("section_id",))
        def finalize(section_id: uuid.UUID, remarks: str) -> str:
            return "hidden-return"

        with caplog.at_level(logging.DEBUG):
            finalize(sec_id, "confidential remarks")

        records = _event_records(caplog, "grades.opted_sync")
        assert len(records) == 2
        for record in records:
            assert record.section_id == sec_id
            formatted = JsonFormatter().format(record)
            assert str(sec_id) in formatted
            assert "confidential remarks" not in formatted
            assert "hidden-return" not in formatted

    @pytest.mark.parametrize(
        "bad_field",
        [
            "password",
            "password_id",
            "token",
            "access_token",
            "student_number",
            "email",
            "birthdate",
            "grade",
            "remarks",
            "full_name",
            "balance",
            "decision",
            "request_id",
            "event",
            "outcome",
        ],
    )
    def test_denied_non_id_or_reserved_field_raises_at_decoration_time(
        self, bad_field: str
    ) -> None:
        with pytest.raises(ValueError):

            @logged("grades.bad_async", fields=(bad_field,))
            async def async_fn(**kwargs: object) -> None:
                pass

        with pytest.raises(ValueError):

            @logged("grades.bad_sync", fields=(bad_field,))
            def sync_fn(**kwargs: object) -> None:
                pass

    def test_string_fields_raises_type_error_at_decoration_time(self) -> None:
        with pytest.raises(TypeError):

            @logged("grades.str_fields", fields="section_id")
            def fn(section_id: str) -> None:
                pass

    def test_unknown_parameter_field_raises_at_decoration_time(self) -> None:
        with pytest.raises(ValueError):

            @logged("grades.missing_param", fields=("section_id",))
            def fn(other_id: str) -> None:
                pass

    def test_grade_id_is_allowed(self) -> None:
        @logged("grades.correction", fields=("grade_id", "grade_correction_id"))
        async def correct(
            *, grade_id: uuid.UUID, grade_correction_id: uuid.UUID
        ) -> None:
            pass

        assert correct.__name__ == "correct"


class TestMetadataPreservation:
    def test_async_preserves_name_doc_and_signature(self) -> None:
        @logged("grades.meta_async", fields=("section_id",))
        async def finalize_grades(
            section_id: uuid.UUID, *, faculty_id: uuid.UUID, dry_run: bool = False
        ) -> bool:
            """Finalize all grades for a section."""
            return not dry_run

        assert finalize_grades.__name__ == "finalize_grades"
        assert finalize_grades.__doc__ == "Finalize all grades for a section."
        assert inspect.iscoroutinefunction(finalize_grades)
        sig = inspect.signature(finalize_grades)
        assert list(sig.parameters) == ["section_id", "faculty_id", "dry_run"]
        assert sig.parameters["dry_run"].default is False

    def test_sync_preserves_name_doc_and_signature(self) -> None:
        @logged("grades.meta_sync", fields=("section_id",))
        def compute_gwa(section_id: uuid.UUID, *, scale: int = 100) -> int:
            """Compute section GWA."""
            return scale

        assert compute_gwa.__name__ == "compute_gwa"
        assert compute_gwa.__doc__ == "Compute section GWA."
        assert not inspect.iscoroutinefunction(compute_gwa)
        sig = inspect.signature(compute_gwa)
        assert list(sig.parameters) == ["section_id", "scale"]
        assert sig.parameters["scale"].default == 100
