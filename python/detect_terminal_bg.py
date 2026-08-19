#!/usr/bin/env python3
"""检测终端背景亮暗 (OSC 11 + WCAG 相对亮度)

用法:
  python3 detect_terminal_bg.py                 # 查询当前终端背景
  python3 detect_terminal_bg.py --hex #1e1f21   # 直接判断给定颜色

参考:
  xterm OSC 11: https://invisible-island.net/xterm/ctlseqs/ctlseqs.html
  XParseColor: https://www.x.org/archive/X11R6.8.2/doc/XParseColor.3.html
  WCAG 相对亮度: https://www.w3.org/WAI/WCAG21/Understanding/relative-luminance.html
"""

import os
import re
import sys
import threading

try:
    import termios
    import tty
except ImportError:
    termios = tty = None

QUERY = b"\x1b]11;?\x1b\\"
REPLY = re.compile(rb"\x1b\]11;rgb:(?P<c>[0-9a-fA-F/]+)(?:\x1b\\|\x07)")
THRESHOLD = 0.5


def comp8(s: bytes) -> int:
    """将 rgb: 分量 (1-4 位十六进制) 缩放为 8 位，短位重复补齐"""
    v = int(s, 16)
    n = len(s)
    if n == 1:
        return (v << 4) | v
    if n == 2:
        return v
    return v >> (4 * (n - 2))


def query(timeout: float = 1.0) -> bytes:
    """发送 OSC 11 查询并返回回复（超时返回空）"""
    # 快速失败：非终端环境
    if not sys.stdout.isatty():
        return b""

    posix = termios is not None
    if posix:
        fd = None
        old = None
        try:
            fd = os.open("/dev/tty", os.O_RDWR)
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            os.write(fd, QUERY)
        except Exception:
            # 若打开 /dev/tty 失败，退回到 stdout
            fd = sys.stdout.fileno()
            os.write(fd, QUERY)
        # 统一线程读取
        result = {}

        def _read():
            buf = b""
            while not REPLY.search(buf):
                try:
                    buf += os.read(fd, 4096)
                except OSError:
                    break
            result["data"] = buf

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout)
        # 恢复终端设置
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if fd is not None and fd != sys.stdout.fileno():
            os.close(fd)
        return result.get("data", b"")
    else:
        # Windows 分支：控制台默认开 ENABLE_LINE_INPUT(0x2)/ENABLE_ECHO_INPUT(0x4)，
        # ReadFile 必须等回车才返回且输入会回显；OSC 回复没有回车，必须关掉
        # ENABLE_VIRTUAL_TERMINAL_INPUT(0x200) 默认关，需开启才能收到 VT 回复
        # 依据: https://learn.microsoft.com/en-us/windows/console/setconsolemode
        import ctypes
        import msvcrt
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_handle = k32.GetStdHandle
        get_handle.restype = wintypes.HANDLE
        get_mode = k32.GetConsoleMode
        get_mode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        set_mode = k32.SetConsoleMode
        set_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        handle = get_handle(-10)  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if get_mode(handle, ctypes.byref(mode)):
            set_mode(
                handle, (mode.value | 0x200 | 0x80) & ~0x6
            )  # 开 VT 输入/扩展标志，关行缓冲/回显
        else:
            mode = None
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)  # 关 CRT 文本模式转换

        fd = sys.stdin.fileno()
        os.write(sys.stdout.fileno(), QUERY)
        result = {}

        def _read():
            buf = b""
            while not REPLY.search(buf):
                try:
                    buf += os.read(fd, 4096)
                except OSError:
                    break
            result["data"] = buf

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout)
        if mode is not None:  # 恢复控制台模式
            set_mode(handle, mode.value)
        return result.get("data", b"")


def luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x 相对亮度 (0=黑, 1=白)"""

    def lin(c: int) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def main() -> int:
    if "--hex" in sys.argv:
        idx = sys.argv.index("--hex")
        if idx + 1 >= len(sys.argv):
            sys.exit("error: --hex 需要颜色值，例如 --hex #1e1f21")
        h = sys.argv[idx + 1]
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", h):
            sys.exit(f"error: --hex 需要 #RRGGBB 格式，收到 {h!r}")
        rgb = tuple(int(h[i : i + 2], 16) for i in (1, 3, 5))
    else:
        m = REPLY.search(query())
        if m is None:
            sys.exit(
                "error: 终端未响应 OSC 11 查询（终端不支持或不在真实终端中运行）；"
                "可用 --hex #RRGGBB 直接判断"
            )
        rgb = tuple(comp8(s) for s in m.group("c").split(b"/"))

    lum = luminance(rgb)
    swatch = f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m  \x1b[0m"
    print(f"{'dark' if lum < THRESHOLD else 'light'}")
    print(f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x} {swatch}")
    print(f"luminance={lum:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
