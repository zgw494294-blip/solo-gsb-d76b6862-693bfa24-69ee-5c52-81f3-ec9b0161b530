"""SQLite-backed stateful protocol-drill sessions.

A session binds one validated protocol to an execution trace: the ordered list
of transition ids already fired, replayed from the protocol's initial
configuration. Replacing the protocol keeps the same trace ids and replays
them against the new transition definitions, which lets a drill continue after
the protocol is edited.

Sessions live in a local SQLite database (env ``SESSIONS_DB``, default
``sessions.db`` in the working directory) holding the protocol, the trace ids
and the version of every session. All worker processes share the same file, so
a session survives service or container rebuilds: it can be queried by its
original id and drilled further, and its version never rolls back.

Every mutation runs inside one ``BEGIN IMMEDIATE`` transaction: the version
check, the trace replay and the write all observe the same committed state, so
two processes modifying one session with the same expected version cannot both
succeed — the loser re-reads the stored version and answers 409. A failed
mutation raises before commit and leaves the stored session untouched. Reads
observe one committed version at a time, and a successful response is built
from the values its own transaction committed, never from later commits.
"""

import json
import os
import sqlite3
import threading
from uuid import uuid4

from fastapi import HTTPException

from .analyzer import (
    apply_transition,
    build_model,
    enabled_transitions,
    is_completion,
    _configuration_view,
)
from .schemas import DeadlockRequest

_DB_PATH = os.environ.get("SESSIONS_DB", "sessions.db")
_BUSY_TIMEOUT_MS = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    protocol TEXT NOT NULL,
    trace_ids TEXT NOT NULL
)
"""

_init_lock = threading.Lock()
_initialized = False


def init_storage() -> None:
    """Create the database directory, file and schema once per process."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        directory = os.path.dirname(os.path.abspath(_DB_PATH))
        os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(_DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            # WAL persists in the database file and lets readers work
            # concurrently with the single writer across processes.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(_SCHEMA)
            connection.commit()
        finally:
            connection.close()
        _initialized = True


def _connect() -> sqlite3.Connection:
    init_storage()
    connection = sqlite3.connect(
        _DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    # Transactions are opened explicitly below; writers that collide with
    # another process wait instead of failing with "database is locked".
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return connection


def create_session(protocol) -> dict:
    """Validate the protocol by building its model, then store it."""
    build_model(protocol)
    session_id = uuid4().hex
    connection = _connect()
    try:
        connection.execute(
            "INSERT INTO sessions (id, version, protocol, trace_ids) VALUES (?, ?, ?, ?)",
            (session_id, 0, _dump_protocol(protocol), "[]"),
        )
    finally:
        connection.close()
    return _session_view(session_id, 0, protocol, [])


def get_session_view(session_id: str) -> dict:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT version, protocol, trace_ids FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown session")
    version, protocol_text, trace_text = row
    return _session_view(
        session_id,
        version,
        DeadlockRequest.model_validate(json.loads(protocol_text)),
        json.loads(trace_text),
    )


def execute_transition(session_id: str, transition_id: str, expected_version: int) -> dict:
    def mutate(version: int, protocol, trace_ids: list[str]):
        if version != expected_version:
            raise _version_conflict(version)

        model = build_model(protocol)
        configuration = _replay(model, trace_ids)

        if is_completion(configuration, model.finals):
            raise HTTPException(
                status_code=409,
                detail={"error": "session_completed", "transition_id": transition_id},
            )

        enabled = enabled_transitions(configuration, model.outgoing)
        if not any(transition.id == transition_id for transition in enabled):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "transition_not_enabled",
                    "transition_id": transition_id,
                    "enabled_transition_ids": [transition.id for transition in enabled],
                },
            )

        return version + 1, protocol, trace_ids + [transition_id]

    return _update_session(session_id, mutate)


def replace_protocol(session_id: str, protocol, expected_version: int) -> dict:
    def mutate(version: int, _stored_protocol, trace_ids: list[str]):
        if version != expected_version:
            raise _version_conflict(version)

        # Replay the already executed ids from the new initial configuration.
        # Every step must be enabled and the trace must not reach a completion
        # configuration before its last step; otherwise the old session stands.
        model = build_model(protocol)
        by_id = {transition.id: transition for transition in model.transitions}

        configuration = model.initial
        for index, transition_id in enumerate(trace_ids, start=1):
            if is_completion(configuration, model.finals):
                raise _replay_failure(index, "configuration already completed")
            transition = by_id.get(transition_id)
            if transition is None:
                raise _replay_failure(index, f"unknown transition id {transition_id!r}")
            if transition not in enabled_transitions(configuration, model.outgoing):
                raise _replay_failure(index, f"transition {transition_id!r} is not enabled")
            configuration = apply_transition(configuration, transition)

        return version + 1, protocol, trace_ids

    return _update_session(session_id, mutate)


def _update_session(session_id: str, mutate) -> dict:
    """Apply ``mutate`` to the stored row inside one immediate transaction.

    ``mutate(version, protocol, trace_ids)`` returns the replacement triple;
    the transaction commits only when it returns, so any HTTPException it
    raises leaves the stored session untouched. ``BEGIN IMMEDIATE`` takes the
    database write lock before the row is read, so the version check, the
    trace replay and the write all run against the same committed state even
    when another process races the same session. The returned view is built
    from the triple this transaction committed, never re-read afterwards.
    """
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT version, protocol, trace_ids FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")
        version, protocol_text, trace_text = row
        protocol = DeadlockRequest.model_validate(json.loads(protocol_text))
        trace_ids = json.loads(trace_text)

        new_version, new_protocol, new_trace_ids = mutate(version, protocol, trace_ids)

        connection.execute(
            "UPDATE sessions SET version = ?, protocol = ?, trace_ids = ? WHERE id = ?",
            (new_version, _dump_protocol(new_protocol), json.dumps(new_trace_ids), session_id),
        )
        connection.commit()
        return _session_view(session_id, new_version, new_protocol, new_trace_ids)
    except BaseException:
        # No-op when the commit already succeeded.
        connection.rollback()
        raise
    finally:
        connection.close()


def _dump_protocol(protocol) -> str:
    return json.dumps(protocol.model_dump(mode="json", by_alias=True))


def _version_conflict(current_version: int) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "version_conflict", "current_version": current_version},
    )


def _replay_failure(index: int, reason: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "replay_failed",
            "first_failed_step": index,
            "reason": reason,
        },
    )


def _replay(model, trace_ids: list[str]):
    """Apply the trace ids; the trace is assumed valid for this model."""
    by_id = {transition.id: transition for transition in model.transitions}
    configuration = model.initial
    for transition_id in trace_ids:
        configuration = apply_transition(configuration, by_id[transition_id])
    return configuration


def _session_view(session_id: str, version: int, protocol, trace_ids: list[str]) -> dict:
    # Every value comes from one stored revision, so protocol, configuration,
    # trace and version always belong to the same committed snapshot.
    model = build_model(protocol)
    by_id = {transition.id: transition for transition in model.transitions}

    configuration = model.initial
    trace = []
    for index, transition_id in enumerate(trace_ids, start=1):
        transition = by_id[transition_id]
        configuration = apply_transition(configuration, transition)
        trace.append(
            {
                "index": index,
                "transition_id": transition.id,
                "from": transition.sender,
                "to": transition.receiver,
                "action": transition.action,
                "message": transition.message,
                **_configuration_view(configuration),
            }
        )

    return {
        "id": session_id,
        "version": version,
        "protocol": protocol,
        "configuration": _configuration_view(configuration),
        "trace": trace,
        "enabled_transition_ids": [
            transition.id
            for transition in enabled_transitions(configuration, model.outgoing)
        ],
    }
