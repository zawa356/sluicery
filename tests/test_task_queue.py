from __future__ import annotations

import pytest

from sluicery.tasks.queue import retry_delay_sec


def test_retry_backoff_is_exponential_capped_and_jittered() -> None:
    assert retry_delay_sec(1, base_sec=60, max_sec=3600, random_fraction=0) == 60
    assert retry_delay_sec(2, base_sec=60, max_sec=3600, random_fraction=0) == 120
    assert retry_delay_sec(3, base_sec=60, max_sec=3600, random_fraction=1) == 264
    assert retry_delay_sec(20, base_sec=60, max_sec=3600, random_fraction=1) == 3600


def test_retry_attempt_must_be_positive() -> None:
    with pytest.raises(ValueError):
        retry_delay_sec(0, base_sec=60, max_sec=3600)
