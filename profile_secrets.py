import ctypes
import json
import shutil
import subprocess
import sys
import uuid


SERVICE_NAME = "codex-ssh-terminal-mcp"


class SecretBackendUnavailable(RuntimeError):
    pass


class SecretNotFound(RuntimeError):
    pass


class MacOSKeychainBackend:
    name = "macos-keychain"
    ERR_SEC_ITEM_NOT_FOUND = -25300
    ERR_SEC_DUPLICATE_ITEM = -25299

    def is_available(self):
        return sys.platform == "darwin"

    def _security(self):
        security = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        security.SecKeychainItemDelete.restype = ctypes.c_int32
        security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        return security

    def _core_foundation(self):
        core = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease.restype = None
        return core

    def _encoded(self, ref, secret=None):
        service = ref["service"].encode("utf-8")
        account = ref["account"].encode("utf-8")
        if secret is None:
            return service, account
        return service, account, secret.encode("utf-8")

    def _find_item(self, ref, include_secret=False):
        security = self._security()
        service, account = self._encoded(ref)
        item = ctypes.c_void_p()
        if include_secret:
            password_len = ctypes.c_uint32()
            password_data = ctypes.c_void_p()
            status = security.SecKeychainFindGenericPassword(
                None,
                len(service),
                service,
                len(account),
                account,
                ctypes.byref(password_len),
                ctypes.byref(password_data),
                ctypes.byref(item),
            )
            if status != 0:
                raise SecretNotFound(f"profile secret not found in macOS Keychain: OSStatus {status}")
            try:
                secret = ctypes.string_at(password_data, password_len.value).decode("utf-8")
            finally:
                security.SecKeychainItemFreeContent(None, password_data)
                if item:
                    self._core_foundation().CFRelease(item)
            return secret

        status = security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            None,
            None,
            ctypes.byref(item),
        )
        if status != 0:
            raise SecretNotFound(f"profile secret not found in macOS Keychain: OSStatus {status}")
        return item

    def store(self, ref, secret):
        security = self._security()
        service, account, secret_data = self._encoded(ref, secret)
        item = ctypes.c_void_p()
        status = security.SecKeychainAddGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            len(secret_data),
            secret_data,
            ctypes.byref(item),
        )
        if status == self.ERR_SEC_DUPLICATE_ITEM:
            existing = self._find_item(ref, include_secret=False)
            try:
                status = security.SecKeychainItemModifyAttributesAndData(
                    existing,
                    None,
                    len(secret_data),
                    secret_data,
                )
            finally:
                if existing:
                    self._core_foundation().CFRelease(existing)
        elif item:
            self._core_foundation().CFRelease(item)
        if status != 0:
            raise RuntimeError(f"failed to store profile secret in macOS Keychain: OSStatus {status}")

    def read(self, ref):
        return self._find_item(ref, include_secret=True)

    def delete(self, ref):
        try:
            item = self._find_item(ref, include_secret=False)
        except SecretNotFound:
            return
        try:
            self._security().SecKeychainItemDelete(item)
        finally:
            if item:
                self._core_foundation().CFRelease(item)


class SecretToolBackend:
    name = "linux-secret-service"
    UNAVAILABLE_HINTS = (
        "Cannot autolaunch D-Bus",
        "No such interface",
        "The name org.freedesktop.secrets",
        "No such secret service",
        "couldn't connect",
        "Could not connect",
        "No session bus",
    )

    def is_available(self):
        return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None

    def _is_service_unavailable(self, stderr):
        return any(hint in (stderr or "") for hint in self.UNAVAILABLE_HINTS)

    def _unavailable_message(self, action, stderr):
        detail = (stderr or "").strip()
        suffix = f" Details: {detail}" if detail else ""
        return (
            f"Linux Secret Service is unavailable while trying to {action}. "
            "Install libsecret/secret-tool and start a Secret Service provider such as "
            "GNOME Keyring or KWallet. WSL usually needs extra DBus/keyring setup."
            + suffix
        )

    def store(self, ref, secret):
        result = subprocess.run(
            [
                "secret-tool",
                "store",
                "--label",
                f"{SERVICE_NAME} {ref['account']}",
                "service",
                ref["service"],
                "account",
                ref["account"],
            ],
            input=secret,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SecretBackendUnavailable(self._unavailable_message("store a profile secret", result.stderr))

    def read(self, ref):
        result = subprocess.run(
            [
                "secret-tool",
                "lookup",
                "service",
                ref["service"],
                "account",
                ref["account"],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0 and self._is_service_unavailable(result.stderr):
            raise SecretBackendUnavailable(self._unavailable_message("read a profile secret", result.stderr))
        if result.returncode != 0 or not result.stdout:
            raise SecretNotFound(result.stderr.strip() or "profile secret not found with secret-tool")
        return result.stdout.rstrip("\n")

    def delete(self, ref):
        subprocess.run(
            [
                "secret-tool",
                "clear",
                "service",
                ref["service"],
                "account",
                ref["account"],
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )


BACKENDS = [MacOSKeychainBackend(), SecretToolBackend()]


def backend_for_name(name):
    for backend in BACKENDS:
        if backend.name == name:
            return backend
    raise SecretBackendUnavailable(f"unsupported secret backend: {name}")


def default_backend():
    for backend in BACKENDS:
        if backend.is_available():
            return backend
    raise SecretBackendUnavailable(
        "No supported system keychain backend is available. macOS requires Security.framework; "
        "Linux/WSL requires secret-tool and a Secret Service provider."
    )


def new_secret_ref(backend=None):
    backend = backend or default_backend()
    return {
        "backend": backend.name,
        "service": SERVICE_NAME,
        "account": "profile:" + uuid.uuid4().hex,
    }


def store_profile_secret(payload, ref=None):
    if ref:
        backend = backend_for_name(ref["backend"])
        if not backend.is_available():
            raise SecretBackendUnavailable(f"secret backend is not available: {backend.name}")
    else:
        backend = default_backend()
        ref = new_secret_ref(backend)
    secret = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    backend.store(ref, secret)
    return ref


def load_profile_secret(ref):
    backend = backend_for_name(ref["backend"])
    if not backend.is_available():
        raise SecretBackendUnavailable(f"secret backend is not available: {backend.name}")
    return json.loads(backend.read(ref))


def delete_profile_secret(ref):
    if not ref:
        return
    backend = backend_for_name(ref["backend"])
    if backend.is_available():
        backend.delete(ref)
