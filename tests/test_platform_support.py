import platform_support


def test_native_windows_is_reported_as_unsupported(monkeypatch):
    monkeypatch.setattr(platform_support.sys, "platform", "win32")

    status = platform_support.platform_status()

    assert status["supported"] is False
    assert status["platform"] == "windows-native"
    assert "WSL" in status["message"]


def test_wsl_is_reported_as_supported(monkeypatch):
    monkeypatch.setattr(platform_support.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    status = platform_support.platform_status()

    assert status["supported"] is True
    assert status["platform"] == "windows-wsl"


def test_unsupported_platform_raises(monkeypatch):
    monkeypatch.setattr(platform_support.sys, "platform", "win32")

    try:
        platform_support.assert_supported_platform()
    except RuntimeError as exc:
        assert "Native Windows is not supported" in str(exc)
    else:
        raise AssertionError("expected unsupported native Windows to raise")
