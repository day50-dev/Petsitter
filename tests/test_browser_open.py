"""When petsitter auto-opens the dashboard, and when it must not."""

import sys
import webbrowser

import pytest

from petsitter import server

GUI_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "BROWSER")


@pytest.fixture
def env(monkeypatch):
    for v in GUI_ENV:
        monkeypatch.delenv(v, raising=False)

    def setup(platform, controller=None, **vars_):
        monkeypatch.setattr(sys, "platform", platform)
        for k, v in vars_.items():
            monkeypatch.setenv(k, v)
        if controller is None:
            controller = webbrowser.BackgroundBrowser("xdg-open")
        monkeypatch.setattr(webbrowser, "get", lambda *a: controller)
        return server._can_open_gui_browser()
    return setup


def test_linux_desktop_opens(env):
    assert env("linux", DISPLAY=":0")[0]
    assert env("linux", WAYLAND_DISPLAY="wayland-0")[0]


def test_linux_console_does_not(env):
    ok, why = env("linux")
    assert not ok and why == "no display"


def test_ssh_does_not_anywhere(env):
    for platform in ("linux", "darwin", "win32"):
        ok, why = env(platform, DISPLAY=":10", SSH_CONNECTION="1.2.3.4 5 6.7.8.9 22")
        assert not ok and why == "running over SSH", platform


@pytest.mark.parametrize("name", ["www-browser", "links", "lynx", "w3m"])
def test_terminal_browsers_are_never_opened(env, name):
    ok, why = env("linux", controller=webbrowser.GenericBrowser(name), DISPLAY=":0")
    assert not ok and name in why


def test_elinks_is_a_terminal_browser_too(env):
    ok, _ = env("linux", controller=webbrowser.Elinks("elinks"), DISPLAY=":0")
    assert not ok


def test_macos_and_windows_need_no_display_variable(env):
    class MacOSXOSAScript(webbrowser.BaseBrowser):   # stand-in: real one only exists on macOS
        def open(self, *a, **k): return True

    class WindowsDefault(webbrowser.BaseBrowser):    # stand-in: real one only exists on Windows
        def open(self, *a, **k): return True

    assert env("darwin", controller=MacOSXOSAScript("default"))[0]
    assert env("win32", controller=WindowsDefault())[0]


def test_explicit_browser_choice_wins(env):
    assert env("linux", BROWSER="firefox")[0]


def test_no_browser_at_all(env, monkeypatch):
    def none(*a):
        raise webbrowser.Error("could not locate runnable browser")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", none)
    ok, why = server._can_open_gui_browser()
    assert not ok and why == "no browser found"
