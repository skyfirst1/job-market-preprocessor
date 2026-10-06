import pytest

from scripts import run_wechat_pool


def test_cli_defaults_to_random_three_to_five_minute_interval():
    policy = run_wechat_pool.pool_policy(run_wechat_pool.parse_args([]))

    assert policy.min_interval_seconds == 180
    assert policy.max_interval_seconds == 300


def test_legacy_interval_remains_a_fixed_interval():
    policy = run_wechat_pool.pool_policy(
        run_wechat_pool.parse_args(["--interval", "45"])
    )

    assert policy.min_interval_seconds == 45
    assert policy.max_interval_seconds == 45


def test_cli_accepts_explicit_interval_range():
    policy = run_wechat_pool.pool_policy(
        run_wechat_pool.parse_args([
            "--min-interval", "180", "--max-interval", "240",
        ])
    )

    assert policy.min_interval_seconds == 180
    assert policy.max_interval_seconds == 240


def test_cli_rejects_mixed_legacy_and_range_options():
    with pytest.raises(SystemExit):
        run_wechat_pool.parse_args([
            "--interval", "180", "--min-interval", "180",
        ])
