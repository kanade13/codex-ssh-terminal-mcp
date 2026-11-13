import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from batch_auth import SharedLoginCode
from password_auth import AskpassBroker
from test_password_auth import helper


def make_broker(name, host, pool, history, ready):
    def publish(kind, text):
        history.append((kind, text))
        if kind == "auth_prompt":
            ready.set()

    broker = AskpassBroker({
        name: {"host": host, "user": "alice", "password": "test-only-password"},
        "jump": {"host": "192.0.2.8", "user": "alice", "password": "test-only-jump"},
    }, publish, timeout=5)
    pool.enroll(broker, ("alice", host))
    return broker


def test_one_code_answers_three_mfa_hosts_while_fourth_uses_only_its_password():
    pool = SharedLoginCode()
    history = []
    ready = threading.Event()
    brokers = [
        make_broker(name, host, pool, history, ready)
        for name, host in (("jump", "192.0.2.8"), ("gpu6", "192.0.2.6"), ("gpu7", "192.0.2.7"))
    ]
    password_host = AskpassBroker({
        "HF3090": {"host": "192.0.2.9", "user": "alice", "password": "test-only-3090"},
    }, lambda kind, text: history.append((kind, text)), timeout=5)
    try:
        with ThreadPoolExecutor() as executor:
            password = executor.submit(helper, password_host, "alice@192.0.2.9's password: ")
            assert password.result(4).stdout == "test-only-3090\n"
            assert password_host.shared_code is None
            assert not pool.accepts(password_host, ("alice", "192.0.2.9"))
            jump = executor.submit(helper, brokers[0], "(alice@192.0.2.8) Verification code: ")
            assert ready.wait(4)
            pool.submit("123456")
            assert jump.result(4).stdout == "123456\n"
            targets = [
                executor.submit(helper, broker, f"(alice@{host}) Verification code: ")
                for broker, host in zip(brokers[1:], ("192.0.2.6", "192.0.2.7"))
            ]
            assert all(result.result(4).stdout == "123456\n" for result in targets)
        assert "123456" not in json.dumps(history)
        assert "test-only-password" not in json.dumps(history)
        assert "test-only-3090" not in json.dumps(history)
    finally:
        pool.close()
        password_host.close()
        for broker in brokers:
            broker.close()
    assert pool._code is None


def test_same_host_retry_waits_for_a_new_code_and_never_sends_cached_code_twice():
    pool = SharedLoginCode()
    ready = threading.Event()
    broker = make_broker("gpu", "192.0.2.6", pool, [], ready)
    try:
        with ThreadPoolExecutor() as executor:
            first = executor.submit(helper, broker, "(alice@192.0.2.6) Verification code: ")
            assert ready.wait(4)
            pool.submit("123456")
            assert first.result(4).stdout == "123456\n"
            ready.clear()
            retry = executor.submit(helper, broker, "(alice@192.0.2.6) Verification code: ")
            assert ready.wait(4)
            assert not retry.done()
            pool.submit("654321")
            assert retry.result(4).stdout == "654321\n"
    finally:
        pool.close()
        broker.close()


@pytest.mark.parametrize("prompt,hint", [
    ("(alice@192.0.2.8) Verification code: ", ""),
    ("(mallory@192.0.2.6) Verification code: ", ""),
    ("(alice@192.0.2.99) Verification code: ", ""),
    ("[sudo] password for alice: ", ""),
    ("Password + OTP: ", ""),
    ("(alice@192.0.2.6) Verification code: ", "confirm"),
])
def test_shared_input_never_answers_unrelated_prompts(prompt, hint):
    pool = SharedLoginCode()
    ready = threading.Event()
    broker = make_broker("gpu", "192.0.2.6", pool, [], ready)
    try:
        with ThreadPoolExecutor() as executor:
            request = executor.submit(helper, broker, prompt, hint)
            assert ready.wait(4)
            assert not broker.shared_code_pending
            with pytest.raises(ValueError, match="没有待处理"):
                pool.submit("123456")
            broker.cancel()
            assert request.result(4).returncode == 1
    finally:
        pool.close()
        broker.close()


def test_expired_code_cannot_be_used_by_a_delayed_target():
    pool = SharedLoginCode()
    ready = threading.Event()
    first = make_broker("gpu6", "192.0.2.6", pool, [], ready)
    delayed = make_broker("gpu7", "192.0.2.7", pool, [], ready)
    try:
        with ThreadPoolExecutor() as executor:
            request = executor.submit(helper, first, "(alice@192.0.2.6) Verification code: ")
            assert ready.wait(4)
            pool.submit("123456")
            assert request.result(4).stdout == "123456\n"
            pool._expire(pool._generation)
            assert pool._code is None
            ready.clear()
            request = executor.submit(helper, delayed, "(alice@192.0.2.7) Verification code: ")
            assert ready.wait(4)
            assert not request.done()
            pool.submit("654321")
            assert request.result(4).stdout == "654321\n"
    finally:
        pool.close()
        first.close()
        delayed.close()


def test_closed_batch_and_non_numeric_input_are_rejected_without_echo():
    pool = SharedLoginCode()
    for value in ("not-a-code-secret", "123456\n", 123456, None, "12345"):
        with pytest.raises(ValueError) as error:
            pool.submit(value)
        assert str(value) not in str(error.value)
    assert not pool.accepts(object(), None)
    assert pool.claim(object(), None) is None
    pool.close()
    with pytest.raises(ValueError):
        pool.submit("123456")
