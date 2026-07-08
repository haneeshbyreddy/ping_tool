from __future__ import annotations

import ctypes
from typing import Callable

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x0, 0x1, 0x2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4

MF_STRING = 0x0000
MF_GRAYED = 0x0001
MF_SEPARATOR = 0x0800

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

MenuItem = tuple[str, bool, Callable[[], None] | None]

def _win():
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    HANDLE = ctypes.c_void_p
    kernel32.GetModuleHandleW.restype = HANDLE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.CreateWindowExW.restype = HANDLE
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        HANDLE, HANDLE, HANDLE, ctypes.c_void_p]
    user32.CreateIcon.restype = HANDLE
    user32.CreateIcon.argtypes = [
        HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_ubyte, ctypes.c_ubyte,
        ctypes.c_char_p, ctypes.c_char_p]
    user32.CreatePopupMenu.restype = HANDLE
    user32.CreatePopupMenu.argtypes = []
    user32.AppendMenuW.restype = ctypes.c_int
    user32.AppendMenuW.argtypes = [
        HANDLE, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.TrackPopupMenu.argtypes = [
        HANDLE, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        HANDLE, ctypes.c_void_p]
    user32.DestroyMenu.argtypes = [HANDLE]
    user32.SetForegroundWindow.argtypes = [HANDLE]
    user32.PostMessageW.argtypes = [
        HANDLE, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.SetTimer.argtypes = [HANDLE, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.c_void_p, HANDLE, ctypes.c_uint, ctypes.c_uint]
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    user32.GetCursorPos.argtypes = [ctypes.c_void_p]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.RegisterWindowMessageW.restype = ctypes.c_uint
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    shell32.Shell_NotifyIconW.restype = ctypes.c_int
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    return user32, kernel32, shell32

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

def _notifyicondata_type():
    from ctypes import wintypes

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        ]

    return NOTIFYICONDATAW

def make_circle_icon(rgb: tuple[int, int, int]) -> int:
    user32, _, _ = _win()
    size, r2 = 16, 7.5 * 7.5
    and_bits = bytearray()
    xor_bits = bytearray()
    for y in range(size):
        row = 0
        for x in range(size):
            inside = (x - 7.5) ** 2 + (y - 7.5) ** 2 <= r2
            row = (row << 1) | (0 if inside else 1)
        and_bits += row.to_bytes(2, "big")
    for y in range(size):
        for x in range(size):
            if (x - 7.5) ** 2 + (y - 7.5) ** 2 <= r2:
                xor_bits += bytes((rgb[2], rgb[1], rgb[0], 255))
            else:
                xor_bits += bytes((0, 0, 0, 0))
    return user32.CreateIcon(None, size, size, 1, 32,
                             bytes(and_bits), bytes(xor_bits))

class TrayApp:

    def __init__(self, *, refresh: Callable[[], tuple[int, str]],
                 build_menu: Callable[[], list[MenuItem]],
                 refresh_ms: int = 5000, window_class: str = "WispTray") -> None:
        from ctypes import wintypes
        self._refresh = refresh
        self._build_menu = build_menu
        self._refresh_ms = refresh_ms
        self._class = window_class
        self._user32, self._kernel32, self._shell32 = _win()
        self._NID = _notifyicondata_type()
        self._hwnd = None
        self._icon_added = False
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
            wintypes.WPARAM, wintypes.LPARAM)
        self._wndproc = WNDPROC(self._on_message)
        self._taskbar_created = self._user32.RegisterWindowMessageW("TaskbarCreated")

    def run(self) -> None:
        from ctypes import wintypes
        user32 = self._user32
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", type(self._wndproc)),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        hinst = self._kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self._class
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError()
        self._hwnd = user32.CreateWindowExW(
            0, self._class, self._class, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            raise ctypes.WinError()

        self._add_or_update_icon(add=True)
        user32.SetTimer(self._hwnd, 1, self._refresh_ms, None)

        msg = ctypes.create_string_buffer(48)
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def quit(self) -> None:
        self._remove_icon()
        self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def _nid(self, hicon: int = 0, tip: str = "") -> object:
        nid = self._NID()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = tip[:127]
        return nid

    def _add_or_update_icon(self, *, add: bool = False) -> None:
        hicon, tip = self._refresh()
        nid = self._nid(hicon, tip)
        if add or not self._icon_added:
            self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            self._icon_added = True
        else:
            self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _remove_icon(self) -> None:
        if self._icon_added and self._hwnd:
            nid = self._nid()
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._icon_added = False

    def _show_menu(self) -> None:
        user32 = self._user32
        items = self._build_menu()
        hmenu = user32.CreatePopupMenu()
        callbacks: dict[int, Callable[[], None]] = {}
        cmd = 1
        for item in items:
            if item is None:
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            label, enabled, cb = item
            flags = MF_STRING | (0 if enabled and cb else MF_GRAYED)
            user32.AppendMenuW(hmenu, flags, cmd, label)
            if cb:
                callbacks[cmd] = cb
            cmd += 1
        pt = _POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)
        chosen = user32.TrackPopupMenu(
            hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            pt.x, pt.y, 0, self._hwnd, None)
        self._user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        cb = callbacks.get(chosen)
        if cb:
            cb()

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY and lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
            self._show_menu()
            return 0
        if msg == WM_TIMER:
            self._add_or_update_icon()
            return 0
        if msg == self._taskbar_created:
            self._icon_added = False
            self._add_or_update_icon(add=True)
            return 0
        if msg == WM_DESTROY:
            self._remove_icon()
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)
