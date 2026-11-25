from __future__ import annotations

import ctypes
import sys
import webbrowser
from ctypes import wintypes as win
from pathlib import Path

from app.http.runtime import settings
from app.launch import stop_service


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, win.HWND, win.UINT, win.WPARAM, win.LPARAM)
ICON_PATH = Path(__file__).resolve().parent / "ui" / "favicon.ico"
WINDOW_CLASS = "ShixuTray"
TRAY_MESSAGE = 0x8001  # WM_APP + 1
WM_CONTEXTMENU = 0x007B
NIM_ADD, NIM_DELETE, NIM_SETFOCUS, NIM_SETVERSION = 0, 2, 3, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 1, 2, 4, 0x80
NOTIFYICON_VERSION = 4
OPEN_PAGE = 1
EXIT_APP = 2
SYNCHRONIZE = 0x00100000
ERROR_ALREADY_EXISTS = 183
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
QS_ALLINPUT = 0x04FF
PM_REMOVE = 1
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x02, 0x100
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
MB_ICONERROR = 0x10


class WindowClass(ctypes.Structure):
    _fields_ = [
        ("style", win.UINT), ("procedure", WNDPROC), ("class_extra", ctypes.c_int),
        ("window_extra", ctypes.c_int), ("instance", win.HINSTANCE), ("icon", win.HICON),
        ("cursor", win.HANDLE), ("background", win.HBRUSH), ("menu_name", win.LPCWSTR),
        ("class_name", win.LPCWSTR),
    ]


class NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("size", win.DWORD), ("window", win.HWND), ("id", win.UINT), ("flags", win.UINT),
        ("message", win.UINT), ("icon", win.HICON), ("tip", win.WCHAR * 128),
        ("state", win.DWORD), ("state_mask", win.DWORD), ("info", win.WCHAR * 256),
        ("version", win.UINT), ("info_title", win.WCHAR * 64), ("info_flags", win.DWORD),
        ("guid", ctypes.c_uint32 * 4), ("balloon_icon", win.HICON),
    ]


def _api(dll: ctypes.WinDLL, name: str, result: type, *args: type):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = args
    return function


user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_create_mutex = _api(kernel32, "CreateMutexW", win.HANDLE, win.LPVOID, win.BOOL, win.LPCWSTR)
_open_process = _api(kernel32, "OpenProcess", win.HANDLE, win.DWORD, win.BOOL, win.DWORD)
_close_handle = _api(kernel32, "CloseHandle", win.BOOL, win.HANDLE)
_get_module = _api(kernel32, "GetModuleHandleW", win.HINSTANCE, win.LPCWSTR)
_wait = _api(user32, "MsgWaitForMultipleObjects", win.DWORD, win.DWORD, ctypes.POINTER(win.HANDLE), win.BOOL, win.DWORD, win.DWORD)
_peek = _api(user32, "PeekMessageW", win.BOOL, ctypes.POINTER(win.MSG), win.HWND, win.UINT, win.UINT, win.UINT)
_dispatch = _api(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(win.MSG))
_default_proc = _api(user32, "DefWindowProcW", LRESULT, win.HWND, win.UINT, win.WPARAM, win.LPARAM)
_register_message = _api(user32, "RegisterWindowMessageW", win.UINT, win.LPCWSTR)
_register_class = _api(user32, "RegisterClassW", win.WORD, ctypes.POINTER(WindowClass))
_unregister_class = _api(user32, "UnregisterClassW", win.BOOL, win.LPCWSTR, win.HINSTANCE)
_create_window = _api(user32, "CreateWindowExW", win.HWND, win.DWORD, win.LPCWSTR, win.LPCWSTR, win.DWORD,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, win.HWND, win.HMENU, win.HINSTANCE, win.LPVOID)
_destroy_window = _api(user32, "DestroyWindow", win.BOOL, win.HWND)
_load_icon = _api(user32, "LoadImageW", win.HICON, win.HINSTANCE, win.LPCWSTR, win.UINT, ctypes.c_int, ctypes.c_int, win.UINT)
_destroy_icon = _api(user32, "DestroyIcon", win.BOOL, win.HICON)
_notify_icon = _api(shell32, "Shell_NotifyIconW", win.BOOL, win.DWORD, ctypes.POINTER(NotifyIconData))
_cursor_pos = _api(user32, "GetCursorPos", win.BOOL, ctypes.POINTER(win.POINT))
_create_menu = _api(user32, "CreatePopupMenu", win.HMENU)
_append_menu = _api(user32, "AppendMenuW", win.BOOL, win.HMENU, win.UINT, ctypes.c_size_t, win.LPCWSTR)
_destroy_menu = _api(user32, "DestroyMenu", win.BOOL, win.HMENU)
_foreground_window = _api(user32, "SetForegroundWindow", win.BOOL, win.HWND)
_track_menu = _api(user32, "TrackPopupMenu", win.BOOL, win.HMENU, win.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   win.HWND, ctypes.POINTER(win.RECT))
_post_message = _api(user32, "PostMessageW", win.BOOL, win.HWND, win.UINT, win.WPARAM, win.LPARAM)
_message_box = _api(user32, "MessageBoxW", ctypes.c_int, win.HWND, win.LPCWSTR, win.LPCWSTR, win.UINT)


def _show_menu(icon_data: NotifyIconData, url: str, pid: int) -> None:
    # Use the click location even when Explorer shows the icon in the overflow panel.
    cursor = win.POINT()
    if not _cursor_pos(ctypes.byref(cursor)):
        return

    menu = _create_menu()
    try:
        _append_menu(menu, 0, OPEN_PAGE, "打开食序网页")
        _append_menu(menu, 0, EXIT_APP, "退出食序")
        # The owner must be in the foreground for the menu to dismiss on outside clicks.
        _foreground_window(icon_data.window)
        command = _track_menu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                              cursor.x, cursor.y, 0, icon_data.window, None)
        _post_message(icon_data.window, 0, 0, 0)  # WM_NULL lets the next popup dismiss correctly.
    finally:
        _destroy_menu(menu)
    if command == OPEN_PAGE:
        webbrowser.open(url)
    elif command == EXIT_APP:
        try:
            stop_service(expected_pid=pid)
        except Exception as exc:
            _message_box(icon_data.window, f"退出食序失败。\n\n{exc}", "食序", MB_ICONERROR)
    else:
        _notify_icon(NIM_SETFOCUS, ctypes.byref(icon_data))


def _run_tray(service: int, url: str, pid: int) -> None:
    taskbar_created = _register_message("TaskbarCreated")
    instance = _get_module(None)
    icon_data = NotifyIconData(size=ctypes.sizeof(NotifyIconData), id=1,
                               flags=NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP,
                               message=TRAY_MESSAGE, version=NOTIFYICON_VERSION, tip="食序")

    def add_icon() -> bool:
        return bool(_notify_icon(NIM_ADD, ctypes.byref(icon_data))
                    and _notify_icon(NIM_SETVERSION, ctypes.byref(icon_data)))

    @WNDPROC
    def window_proc(window: int, message: int, wparam: int, lparam: int) -> int:
        if message == TRAY_MESSAGE and (lparam & 0xFFFF) == WM_CONTEXTMENU:
            _show_menu(icon_data, url, pid)
            return 0
        if message == taskbar_created:
            add_icon()
            return 0
        return _default_proc(window, message, wparam, lparam)

    window_class = WindowClass(procedure=window_proc, instance=instance, class_name=WINDOW_CLASS)
    if not _register_class(ctypes.byref(window_class)):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        icon_data.window = _create_window(0, WINDOW_CLASS, "食序", 0, 0, 0, 0, 0, None, None, instance, None)
        if not icon_data.window:
            raise ctypes.WinError(ctypes.get_last_error())
        icon_data.icon = _load_icon(None, str(ICON_PATH), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not icon_data.icon:
            raise ctypes.WinError(ctypes.get_last_error())
        if not add_icon():
            raise RuntimeError("Windows could not register the Shixu notification icon")

        handles = (win.HANDLE * 1)(service)
        message = win.MSG()
        while True:
            ready = _wait(1, handles, False, INFINITE, QS_ALLINPUT)
            if ready == 0:
                break
            if ready == WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            while _peek(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                _dispatch(ctypes.byref(message))
    finally:
        if icon_data.window:
            _notify_icon(NIM_DELETE, ctypes.byref(icon_data))
            _destroy_window(icon_data.window)
        if icon_data.icon:
            _destroy_icon(icon_data.icon)
        _unregister_class(WINDOW_CLASS, instance)


def main(pid: int) -> None:
    mutex = _create_mutex(None, False, f"Local\\ShixuTray-{pid}")
    error = ctypes.get_last_error()
    if not mutex:
        raise ctypes.WinError(error)
    try:
        if error == ERROR_ALREADY_EXISTS:
            return
        service = _open_process(SYNCHRONIZE, False, pid)
        if not service:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            _run_tray(service, f"{settings().origin}/", pid)
        finally:
            _close_handle(service)
    finally:
        _close_handle(mutex)


if __name__ == "__main__":
    main(int(sys.argv[1]))
