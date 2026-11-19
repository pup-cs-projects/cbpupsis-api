# Issue #146 acceptance evidence

Automated grades/transcript coverage for AC-STU-004.1 through .17.
Tested against merged `fr004` at
`9645e1ac13145a5cca28545f3dbaf3a811716abe`.

Tests and coverage documentation only: no API, permission, configuration or
migration changes. All data and uploaded PDF bytes are synthetic.
Student routes use `/api/v1/student` from #147.

## AC-to-test matrix

Every test below starts with `test_ac_stu_004_`. Suffixes identify exact tests.
Read-side HTTP tests are in `test_acceptance.py`; arithmetic tests are in
`../grading/test_acceptance_calculations.py`; producer integration tests are in
`test_producer_integration.py`.

| AC-STU-004 | Test suffixes | Evidence |
|---|---|---|
| .1 | `1_latest_posted_term_has_complete_course_fields` | HTTP course code/title/units/mark/status; latest posted term, not newer draft-only term. |
| .2 | `2_http_gwa_excludes_nstp_inc_and_w`; `2_three_unit_nstp_does_not_change_gwa`; `2_nonnumeric_marks_are_excluded`; `2_averages_are_decimal_and_round_half_up` | Decimal arithmetic and service averages; 1.00/5.00 endpoints, numeric failure weight, 3-unit NSTP/nonnumeric exclusion, half-up rounding. |
| .3 | `3_real_finalize_creates_365_day_inc_deadline`; `3_inc_displays_stored_365_day_deadline_and_is_neutral`; `3_inc_is_neither_pass_nor_fail_and_earns_no_units` | Genuine faculty password/TOTP draft/finalize HTTP flow creates the stored deadline; student reads it exactly 365 days after posting, no GWA/earned units. Separate stored-read case covers leap-day arithmetic. |
| .4 | `4_w_is_visible_but_earns_no_units_or_gwa`; `4_withdrawal_contributes_no_average_or_earned_units` | W displayed as Withdrawn, excluded from grades/transcript GWA and credits. |
| .5 | `5_transcript_orders_terms_and_uses_weighted_record_totals` | Newest-first terms/courses, term averages, weighted full-record GWA and credits. |
| .6 | `6_no_grades_is_200_with_explanation_and_null_gwa`; `6_empty_average_is_none_not_zero` | 200, empty courses, explanatory message, null GWA. |
| .7 | `7_real_correction_approval_updates_student_grade_and_gwa`; `7_persisted_correction_is_reread_and_gwa_recomputed` | Real faculty finalization and multipart correction filing; three distinct authenticated/TOTP Chair, Dean and Registrar sessions with real IAM/catalog scopes. Proposed mark stays hidden until Registrar commit; student grades and transcript then show new mark/recomputed term and cumulative GWA. Separate SQL regression checks fresh reads despite loaded ORM state. |
| .8 | `8_another_students_grades_are_not_found` | Actual B profile/grade excluded from A's self grades/transcript; removed identified URL is 404; independent service owner/number mismatch is RESOURCE_NOT_FOUND. |
| .9 | `9_unpaired_term_filters_are_refused`; `9_paired_filters_select_history_not_latest` | Unpaired filters 422, neither selects latest posted term, paired filters select history. |
| .10 | `10_unpublished_marks_are_neither_shown_nor_counted` | Draft/unlocked/unsubmitted marks excluded from courses and averages. Producer .3 also proves a real draft remains hidden. |
| .11 | `11_stored_standing_and_reduction_are_not_guessed`; `11_suspension_is_rejected_not_served_as_a_standing` | All six stored tiers/reductions, unevaluated null/message, no inferred standing or stored-GPA substitution; corrupt Suspension rejected. |
| .12 | `12_paging_defaults_and_totals_cover_the_whole_record`; `12_page_size_boundaries` | 21 terms split 20/1, nextPage, no duplication, whole-record totals; limits 0/1/100/101. |
| .13 | `13_unended_terms_have_gwa_but_not_cumulative_weight` | Ends today/in future: own GWA visible, no cumulative weight; completed history counts. |
| .14 | `14_repeats_count_both_marks_but_credits_only_once` | Same passed course ID across two terms: both attempts/marks count toward GWA, credits once. |
| .15 | `15_pdf_has_identity_all_terms_totals_and_page_numbers` | Real multipage renderer/pypdf: header, identity, unofficial label, all 21 terms beyond JSON page size, totals, page numbers and generic filename. Not a visual-layout audit. |
| .16 | `16_invalid_records_fail_without_partial_results` | Invalid numeric mark, NaN and missing Inc deadline across grades/transcript/PDF: atomic 503 GRADE_RECORD_UNAVAILABLE with temporary-unavailability message, no partial records/PDF. |
| .17 | `17_faculty_session_is_404_resource_not_found` | Genuine faculty password/TOTP session holding both read permissions gets 404 RESOURCE_NOT_FOUND on grades/transcript/PDF. |

## Real boundaries and limitations

Only the repository database dependency is substituted with in-memory SQLite.
The producer integration tests use genuine faculty and three distinct Admin
password/TOTP sessions, real IAM/catalog scopes, multipart PDF validation,
ordered Chair/Dean/Registrar decisions, and real student HTTP rereads. No auth,
permission, producer, clock, approval, calculation or renderer behavior is mocked.

The merged baseline includes grade finalization, Inc handling and Correction of
Entry from #177/#178/#103. Migration metadata is reconciled to runtime head
m103_corrections_profile. Metadata checks do not prove database rollout.
SQLite tests do not prove PostgreSQL concurrency, migrations or deployment.

## Validation (2026-11-19)

- Required grading/student_grades suite: **145 passed, 2 skipped in 317.33
  seconds**, via
  `rtk proxy uv run rtk pytest tests/domains/student_grades tests/domains/grading -q --tb=short`.
  Both skips are opt-in #103 PostgreSQL tests; no #146 test skipped or xfailed.
- `tests/core/test_runtime_head.py`: **3 passed in 4.11 seconds**.
- Affected-file Ruff check and format --check passed. Named tests cover AC 1-17.
- Negative control: omitting NSTP exclusion via an external runtime-only pytest
  plugin caused the targeted NSTP assertion to fail as expected (1 failed).
  The unchanged normal implementation passed the same case (1 passed).
  No production source was modified.
- PostgreSQL opt-in variables were cleared for the suite; no external database
  or migrations were used.
- Full #146 CI has not run or been observed. The CI workflow does not automatically
  run for fr004 PRs; publication and explicit CI execution remain delivery gates.
