"""A short-lived OTP shared only by explicitly enrolled login brokers."""
import re
import threading


class SharedLoginCode:
    def __init__(self, ttl=30):
        self.ttl = ttl
        self.lock = threading.RLock()
        self.brokers = {}
        self.allowed = {}
        self._code = None
        self._used = set()
        self._timer = None
        self._generation = 0
        self._closed = False

    def enroll(self, broker, identity):
        with self.lock:
            if self._closed:
                raise ValueError("本次批量认证已经结束")
            self.brokers[id(broker)] = broker
            self.allowed[id(broker)] = identity
        broker.shared_code = self

    def submit(self, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("请输入当前的六位动态验证码")
        with self.lock:
            brokers = list(self.brokers.values())
            closed = self._closed
        # Never broadcast to a shell, password prompt or an unregistered host.
        if closed or not any(broker.shared_code_pending for broker in brokers):
            raise ValueError("当前没有待处理的验证码请求，请等待连接提示")
        with self.lock:
            if self._closed:
                raise ValueError("本次批量认证已经结束")
            if self._timer:
                self._timer.cancel()
            self._code = code
            self._used.clear()
            self._generation += 1
            generation = self._generation
            self._timer = threading.Timer(self.ttl, self._expire, (generation,))
            self._timer.daemon = True
            self._timer.start()
        for broker in brokers:
            broker.deliver_shared_code()

    def accepts(self, broker, identity):
        with self.lock:
            return not self._closed and id(broker) in self.allowed and self.allowed[id(broker)] == identity

    def claim(self, broker, identity):
        with self.lock:
            if (
                self._closed or not self._code
                or id(broker) not in self.allowed
                or self.allowed[id(broker)] != identity or identity in self._used
            ):
                return None
            self._used.add(identity)
            return self._code

    def _expire(self, generation):
        with self.lock:
            if generation == self._generation:
                self._code = None

    def close(self):
        with self.lock:
            self._closed = True
            self._code = None
            self._used.clear()
            if self._timer:
                self._timer.cancel()
            self.brokers.clear()
            self.allowed.clear()
