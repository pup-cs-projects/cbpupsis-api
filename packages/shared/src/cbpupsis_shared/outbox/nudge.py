"""Nudging the worker Lambda right after a commit that staged outbox messages.

On AWS the worker is a scheduled Lambda (``worker.lambda_handler``). A Lambda
cannot hold a LISTEN connection, so :func:`~cbpupsis_shared.outbox.notify_wakeup`
cannot reach it, and a short schedule was the only way to keep emails quick. That
schedule kept Neon awake around the clock: the database only sleeps after five
idle minutes, and a drain every minute never gave it five.

So the API invokes the worker itself, asynchronously, once per request that
committed at least one outbox message. The schedule then only has to be a slow
fallback sweep for the rare nudge that fails.

How a request comes to owe a nudge:

1. :func:`~cbpupsis_shared.outbox.publish_transactional` calls :func:`mark_staged`.
2. When that session commits, :func:`_settle_on_commit` turns "staged" into
   "owed". When it rolls back, :func:`_forget_on_rollback` drops "staged", so a
   rolled-back request never wakes the worker for nothing.
3. ``get_db`` runs :func:`nudge_worker` when the request finishes. It invokes the
   worker at most once, however many messages were committed.

Like ``notify_wakeup`` this is an optimisation, never the delivery mechanism.
The message is already durable when the nudge runs, so every failure here is
logged and swallowed: raising would fail a request whose work has already
committed. The worst case of a lost nudge is one schedule interval of latency.

What does NOT nudge:

- The worker itself. It never goes through ``get_db``, so the messages its
  handlers stage are picked up by its own next pass or by the sweep.
- Sessions a service opens outside ``get_db``. Those messages wait for the sweep.
- Any environment without ``outbox_worker_function_name`` (local, tests, CI).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

import boto3
from botocore.config import Config
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from cbpupsis_core.config import settings
from cbpupsis_database.session import REQUEST_FINISHED_HOOKS

logger = logging.getLogger(__name__)

#: ``Session.info`` key: this transaction staged an outbox message.
STAGED_KEY = "outbox_nudge_staged"
#: ``Session.info`` key: a transaction that staged a message has committed.
OWED_KEY = "outbox_nudge_owed"

#: Short, because the request waits for the call before Lambda returns the
#: response. One attempt, because the sweep is the retry.
INVOKE_TIMEOUT_SECONDS = 2


def mark_staged(session: Session | AsyncSession) -> None:
    """Note that this session's open transaction staged an outbox message."""
    session.info[STAGED_KEY] = True


@sa_event.listens_for(Session, "after_commit")
def _settle_on_commit(session: Session) -> None:
    if session.info.pop(STAGED_KEY, False):
        session.info[OWED_KEY] = True


@sa_event.listens_for(Session, "after_rollback")
def _forget_on_rollback(session: Session) -> None:
    session.info.pop(STAGED_KEY, None)


@functools.cache
def _lambda_client() -> Any:
    """One client per process, built on first use so tests never reach AWS."""
    return boto3.client(
        "lambda",
        config=Config(
            connect_timeout=INVOKE_TIMEOUT_SECONDS,
            read_timeout=INVOKE_TIMEOUT_SECONDS,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def _invoke(function_name: str) -> None:
    # "Event" queues the invocation and returns 202 at once: the request does
    # not wait for the drain, only for Lambda to accept it.
    _lambda_client().invoke(
        FunctionName=function_name, InvocationType="Event", Payload=b"{}"
    )


async def nudge_worker(session: AsyncSession) -> None:
    """Invoke the worker once if this session committed an outbox message.

    Runs before the Lambda handler returns, on purpose: Lambda freezes the
    process as soon as the response is handed back, so a nudge scheduled to run
    "after the response" would sit frozen until the next request.
    """
    if not session.info.pop(OWED_KEY, False):
        return
    function_name = settings.outbox_worker_function_name
    if not function_name:
        return
    try:
        await asyncio.to_thread(_invoke, function_name)
    except Exception:  # a lost nudge costs latency, never a message
        logger.warning(
            "outbox.nudge.failed; the scheduled sweep will deliver it",
            exc_info=True,
        )


if nudge_worker not in REQUEST_FINISHED_HOOKS:
    REQUEST_FINISHED_HOOKS.append(nudge_worker)
