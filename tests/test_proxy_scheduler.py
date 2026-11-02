"""Auto-verify scheduling is owned, wakeable and never reports launch as success."""
import threading

import pytest

from zcode_gateway.generator import ProxyChecker


@pytest.mark.parametrize('enabled,interval', [(True, '5'), (True, 0), (True, 4),
    (True, 10081), (True, float('nan')), (True, True), ('false', 45)])
def test_invalid_auto_settings_do_not_change_state(tmp_path, enabled, interval):
    checker = ProxyChecker(tmp_path, python_exe='/usr/bin/python3')
    before = checker.auto_status()
    with pytest.raises(ValueError):
        checker.set_auto_verify(enabled, interval)
    assert checker.auto_status() == before
    assert checker._auto_thread is None


class WakeProbe:
    def __init__(self):
        self.event = threading.Event()
        self.condition = threading.Condition()
        self.intervals = []

    def set(self):
        self.event.set()

    def clear(self):
        self.event.clear()

    def wait(self, timeout):
        with self.condition:
            self.intervals.append(timeout)
            self.condition.notify_all()
        return self.event.wait(2)

    def observed(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.intervals) >= count, 2)


def test_interval_change_wakes_same_owned_thread_and_disable_stops_it(tmp_path):
    checker = ProxyChecker(tmp_path, python_exe='/usr/bin/python3')
    wake = WakeProbe()
    checker._auto_wake = wake
    checker.run = lambda *args, **kwargs: pytest.fail('No real-time interval expired')
    try:
        checker.set_auto_verify(True, 45)
        wake.observed(1)
        thread = checker._auto_thread
        checker.set_auto_verify(True, 180)
        wake.observed(2)
        assert checker._auto_thread is thread
        assert wake.intervals[:2] == [45 * 60, 180 * 60]
        assert checker.auto_status()['interval_min'] == 180
        assert checker.last_verified_at is None
    finally:
        checker.set_auto_verify(False)
        thread = checker._auto_thread or thread
        thread.join(2)
        assert not thread.is_alive()
    assert checker._auto_thread is None and checker._auto_stop.is_set()


def test_rapid_disable_enable_does_not_lose_scheduler(tmp_path):
    checker = ProxyChecker(tmp_path, python_exe='/usr/bin/python3')
    wake = WakeProbe()
    checker._auto_wake = wake
    calls = []
    checker.run = lambda *args, **kwargs: calls.append(args)
    checker.set_auto_verify(True, 45)
    wake.observed(1)
    first = checker._auto_thread
    checker.set_auto_verify(False)
    checker.set_auto_verify(True, 30)
    wake.observed(2)
    current = checker._auto_thread
    assert current is not None and current.is_alive()
    assert checker.auto_verify and not checker._auto_stop.is_set()
    checker.set_auto_verify(False)
    for thread in {first, current}:
        thread.join(2)
        assert not thread.is_alive()
    assert not calls


def test_old_timer_generation_cannot_start_checker(tmp_path):
    checker = ProxyChecker(tmp_path, python_exe='/usr/bin/python3')
    assert 'error' in checker.run('recheck', _auto_generation=checker._auto_generation)
    assert checker.proc is None


def test_shutdown_does_not_create_a_scheduler(tmp_path):
    checker = ProxyChecker(tmp_path, python_exe='/usr/bin/python3')
    checker._shutdown = True
    assert checker.set_auto_verify(True, 20)['auto_verify'] is False
    assert checker._auto_thread is None


def test_auto_launch_does_not_claim_completed_verification(tmp_path):
    checker = ProxyChecker(tmp_path, python_exe='/usr/bin/python3')
    class ImmediateOnce:
        def clear(self):
            pass
        def wait(self, timeout):
            return False
    checker._auto_wake = ImmediateOnce()
    checker.auto_verify = True
    calls = []
    def run(action, **kwargs):
        calls.append(action)
        checker.auto_verify = False
        return {'started': action}
    checker.run = run
    checker._auto_loop()
    assert calls == ['recheck']
    assert checker.last_verified_at is None
