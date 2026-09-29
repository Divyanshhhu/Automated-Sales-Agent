import pytest
import requests

from src import retry
from src.retry import call_with_retry, is_retriable_requests_error


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry.time, "sleep", lambda _: None)


def _http_error(status: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.exceptions.HTTPError(response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (requests.exceptions.Timeout(), True),
        (requests.exceptions.ConnectionError(), True),
        (_http_error(429), True),
        (_http_error(503), True),
        (_http_error(401), False),
        (_http_error(404), False),
        (ValueError(), False),
    ],
)
def test_is_retriable(exc: Exception, expected: bool) -> None:
    assert is_retriable_requests_error(exc) is expected


def test_retries_then_succeeds() -> None:
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise requests.exceptions.Timeout()
        return "ok"

    assert call_with_retry(flaky, is_retriable=is_retriable_requests_error) == "ok"
    assert len(attempts) == 3


def test_gives_up_after_max_attempts() -> None:
    attempts: list[int] = []

    def always_down() -> None:
        attempts.append(1)
        raise requests.exceptions.Timeout()

    with pytest.raises(requests.exceptions.Timeout):
        call_with_retry(always_down, is_retriable=is_retriable_requests_error, max_attempts=3)
    assert len(attempts) == 3


def test_non_retriable_error_raises_immediately() -> None:
    attempts: list[int] = []

    def auth_failure() -> None:
        attempts.append(1)
        raise _http_error(401)

    with pytest.raises(requests.exceptions.HTTPError):
        call_with_retry(auth_failure, is_retriable=is_retriable_requests_error)
    assert len(attempts) == 1
