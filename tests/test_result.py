"""Result / AppError semantics (design doc 02, section 2.1)."""

from __future__ import annotations

import pytest

from book_translator.domain.result import (
    AppError,
    Err,
    ErrorCode,
    ErrorScope,
    Ok,
    UnwrapError,
    err,
    is_err,
    is_ok,
    map_ok,
    unwrap,
)


def _error() -> AppError:
    return AppError(code=ErrorCode.INTERNAL, message="boom", scope=ErrorScope.JOB_FATAL)


def test_ok_and_err_predicates() -> None:
    assert Ok(1).is_ok() is True
    assert Ok(1).is_err() is False
    assert Err(_error()).is_ok() is False
    assert Err(_error()).is_err() is True


def test_type_guards() -> None:
    ok: Ok[int] = Ok(5)
    bad = Err(_error())
    assert is_ok(ok) and not is_err(ok)
    assert is_err(bad) and not is_ok(bad)


def test_unwrap() -> None:
    assert unwrap(Ok("x")) == "x"
    with pytest.raises(UnwrapError) as info:
        unwrap(Err(_error()))
    assert info.value.error.code is ErrorCode.INTERNAL
    assert "[internal/job_fatal] boom" in str(info.value)


def test_map_ok() -> None:
    assert map_ok(Ok(2), lambda v: v * 3) == Ok(6)
    failure = Err(_error())
    assert map_ok(failure, lambda v: v) is failure


def test_err_helper_and_frozen() -> None:
    result = err(
        ErrorCode.PROVIDER_RATE_LIMITED,
        "slow down",
        ErrorScope.CHUNK_RETRYABLE,
        retry_after_s=2.5,
        context={"chunk_id": 7},
        cause="HTTPError: 429",
    )
    assert result.error.retry_after_s == 2.5
    assert result.error.context == {"chunk_id": 7}
    assert result.error.code.value == "provider_rate_limited"
    with pytest.raises(AttributeError):
        result.error.message = "changed"  # type: ignore[misc]


def test_error_codes_include_sqlite_unsupported() -> None:
    assert ErrorCode.SQLITE_UNSUPPORTED.value == "sqlite_unsupported"
    assert len({c.value for c in ErrorCode}) == len(list(ErrorCode))
