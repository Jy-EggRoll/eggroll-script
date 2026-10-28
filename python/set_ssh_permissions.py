#!/usr/bin/env python3
"""收紧 SSH 目录及其下文件的访问权限

用法:
  python3 set_ssh_permissions.py                      # 预演：只列出将要执行什么
  python3 set_ssh_permissions.py --apply              # 真正改写权限
  python3 set_ssh_permissions.py --check              # 只检测，不合规即非零退出（仅 POSIX）
  python3 set_ssh_permissions.py --ssh-dir /tmp/x/.ssh
  python3 set_ssh_permissions.py --follow-links       # 符号链接改为处理它指向的真实文件
  python3 set_ssh_permissions.py --log-level trace

安全边界（这几条是刻意的限制，不是功能缺失）:
  - 只处理 SSH 目录与其下的普通文件。子目录不递归，符号链接默认跳过：链接指向的文件
    往往在 SSH 目录之外，顺着改权限就是越界改动。要用 --follow-links 显式开启，
    开启后只跟进“指向普通文件”的链接，目录链接与损坏链接仍然跳过
  - 把配置集中放在别处（例如 git 仓库）再用链接挂进 ~/.ssh 是常见做法，但 ssh 校验的是
    链接目标的 ACL，脚本跳过链接时它就会一直是坏的。--follow-links 就是为这种结构准备的
  - POSIX 目录取 0700、文件一律取 0600，不按私钥、.pub、config 分级。分级要靠文件名去猜，
    猜错就是把私钥留成别人可读；这几个文件的安全下界本来就是“只有本人可读”
  - Windows 也处理 SSH 目录本身。sshd 侧的目录检查确实只覆盖 ProgramData\\ssh，用户
    .ssh 目录不在其校验路径上；但客户端会校验 ~/.ssh/config 等文件，而目录上宽泛的继承项
    会被新生成的文件继承下去（实测：继承到 Authenticated Users:(M) 的 config 会直接让
    ssh 以 “Bad permissions / Bad owner or permissions” 拒绝启动）。断开目录的继承并重新
    授权，才能避免日后新建密钥、重写 config 时再次踩同一个坑
  - Windows 不读取现有 ACL。要判断“是否已合规”就得解析 icacls 或 PowerShell 的输出，
    前者是本地化显示名、继承项带标记，后者依赖 Microsoft.PowerShell.Security 模块可用
    （实测 Windows PowerShell 5.1 上该模块可能加载失败）。两者都会把“读不到”伪装成
    “需要改”，所以这里一律直接施加目标 ACL；因此 Windows 上不支持 --check
  - Windows 只走 icacls，绝不退化成 os.chmod：NTFS 的访问控制由 ACL 承载，chmod 只能
    改到只读标志位，看起来成功了，实际没有保护
  - 属主不是当前用户时只告警，绝不 chown。改属主要特权，而且会牵动备份、还原链与 sudo 用法
  - 改动后回读确认。FAT、部分网络盘与容器挂载上 chmod 可能既不报错也不生效
  - 授权项里的主体一律用 SID（icacls 要求数值 SID 带 * 前缀），避开本地化账户名与
    个人微软账户的解析问题

退出码:
  0  正常结束（预演、全部合规、执行成功都算正常）
  1  SSH 目录不存在、不是目录，或 --ssh-dir 为空/非法
  2  有条目未能成功修改；--check 发现不合规项
  3  平台不支持；Windows 上使用 --check；找不到 icacls；拿不到当前用户的 SID
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SSH_DIR_NAME = ".ssh"

POSIX_DIR_MODE = 0o700
POSIX_FILE_MODE = 0o600

# Windows 授权表：主体 -> 权限串。主体用 SID，权限串用简单权限 F；icacls 对数值 SID
# 要求 * 前缀，权限串裸写不带括号（括号形式只用于 DE/RC 这类高级权限）。
WINDOWS_GRANTEES = (
    ("*S-1-5-18", "(F)"),        # NT AUTHORITY\SYSTEM：sshd 以它读取 authorized_keys
    ("*S-1-5-32-544", "(F)"),    # BUILTIN\Administrators：保住管理入口，也在 sshd 白名单内
)

WINDOWS_SETOWNER_HINT = "/setowner"
WINDOWS_SYSTEM_ROOT_FALLBACK = "C:\\Windows"
WINDOWS_WHOAMI_ARGS = ("/user", "/fo", "csv", "/nh")

EXIT_OK = 0
EXIT_BAD_PATH = 1
EXIT_INCOMPLETE = 2
EXIT_UNSUPPORTED = 3

MODE_PREVIEW = "preview"
MODE_APPLY = "apply"
MODE_CHECK = "check"

MODE_TEXT = {
    MODE_PREVIEW: ("预演（只打印计划，不改动任何权限）", "待改", "待改动"),
    MODE_APPLY: ("执行（会改写权限）", "已改", "已改动"),
    MODE_CHECK: ("检查（只报告，不改动任何权限）", "不合规", "不合规"),
}

TRACE = 5
LOG_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# Windows 控制台默认把 ANSI 序列当普通字符打出来，必须先打开这个模式位。
VT_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11

log = logging.getLogger("ssh_perms")


class Palette:
    """ANSI 着色开关集中在一处，关掉时所有调用退化为原文，调用点不必写条件。"""

    CODES = {
        "reset": "\x1b[0m",
        "bold": "\x1b[1m",
        "dim": "\x1b[2m",
        "red": "\x1b[31m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "cyan": "\x1b[36m",
    }

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        return "".join(self.CODES[s] for s in styles) + text + self.CODES["reset"]


def enable_ansi() -> bool:
    """判断能不能安全输出颜色；判断失败一律当作不能，避免满屏乱码。"""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_handle, get_mode, set_mode = k32.GetStdHandle, k32.GetConsoleMode, k32.SetConsoleMode
        get_handle.restype = wintypes.HANDLE
        get_handle.argtypes = [wintypes.DWORD]
        get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        set_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = get_handle(wintypes.DWORD(STD_OUTPUT_HANDLE & 0xFFFFFFFF))
        mode = wintypes.DWORD()
        if not get_mode(handle, ctypes.byref(mode)):
            return False
        if mode.value & VT_PROCESSING:
            return True
        return bool(set_mode(handle, wintypes.DWORD(mode.value | VT_PROCESSING)))
    except Exception:
        # 老系统、被重定向的句柄、受限容器里都可能失败；没有颜色也能用。
        return False


@dataclass(frozen=True)
class Target:
    """一处待处理的路径。目录与文件的目标权限不同，由 is_directory 区分。"""

    path: Path
    is_directory: bool

    @property
    def desired_mode(self) -> int:
        return POSIX_DIR_MODE if self.is_directory else POSIX_FILE_MODE


@dataclass
class Stats:
    """计数口径与摘要一一对应，避免“跳过”里混进因错误跳过的条目。"""

    skipped: int = 0
    changed: int = 0
    failed: int = 0
    warnings: int = 0


class HomeLookupError(RuntimeError):
    """家目录无法确定时的信号，由 main 转成退出码 1。"""


def format_mode(mode: int) -> str:
    return format(mode, "04o")


def describe(target: Target) -> str:
    """目录显示完整路径（要确认动的是哪个目录），文件只显示文件名。"""
    if target.is_directory:
        return "目录 " + str(target.path)
    return "文件 " + target.path.name


def run_quiet(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    """统一子进程口径：列表传参、不经 shell、按本地编码解码、成败只看退出码。

    encoding="locale" 让中文 Windows 上的 GBK 输出正确解码；errors="replace" 保证个别
    编不出来的字符不会把整条流程打断，日志里带着真实路径，生僻字难以避免。
    """
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="locale",
            errors="replace",
            shell=False,
        )
    except OSError as exc:
        log.error("无法执行 %s：%s", command[0], exc)
        return None


def plain_path(path: Path) -> Path:
    r"""去掉 Windows 的扩展长度前缀。

    Path.resolve() 在 Windows 上返回 \\?\D:\... 这种扩展长度形式，chmod 不在乎，
    但 icacls 与 whoami 这类外部程序不认这个前缀，直接传过去会失败。写回普通形式让
    两条平台分支拿到的都是外部程序能接受的样子。UNC 形式 \\?\UNC\server\share 要还原成
    \\server\share，不是简单删掉前缀。
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\") :]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\") :]
    return Path(text)


def resolve_ssh_dir(override: str | None) -> Path:
    """确定要处理的目录：显式路径优先，否则取当前用户家目录下的 .ssh。

    家目录用 Path.home() 而不是读环境变量：原版取 USERPROFILE，在非 Windows 上直接
    KeyError 崩溃，在改过环境变量的 shell 里则会安静地处理错位置。但 Path.home() 本身
    也可能给出无意义结果（Windows 上相关变量全缺时 CPython 返回字面量 "~"），必须拦住。
    显式传入的路径先 resolve()：若它是符号链接，解析后改动与回读看的是同一个真实路径，
    否则 chmod 改的是目标、回读看的是链接本身，会永远报“未达到预期”。
    """
    if override is not None:
        if not override.strip():
            raise HomeLookupError("--ssh-dir 的路径为空")
        return plain_path(Path(override).expanduser().resolve())
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise HomeLookupError(f"无法确定家目录：{exc}") from exc
    if not home.is_absolute() or str(home) == "~":
        raise HomeLookupError(
            f"家目录解析结果不可用（{home}），请用 --ssh-dir 显式指定要处理的目录"
        )
    return plain_path((home / SSH_DIR_NAME).resolve())


def collect_targets(
    ssh_dir: Path, include_directory: bool, stats: Stats, follow_links: bool
) -> list[Target]:
    """列出待处理的普通文件，其余条目记进 stats 并说明原因。

    判定走 lstat：符号链接跳过（chmod 会顺着链接改到目标，那个文件很可能在 SSH 目录
    之外），普通目录不递归。目录不可读时不能裸抛栈——那是脚本自己能修好的状态（把目录
    改回 0700 即可），抛栈会让使用者看到一个和“路径非法”混淆的退出码。
    """
    targets: list[Target] = []
    if include_directory:
        targets.append(Target(ssh_dir, True))
    try:
        entries = list(os.scandir(ssh_dir))
    except OSError as exc:
        log.error("无法读取目录 %s：%s", ssh_dir, exc)
        raise
    # 两条路径可能指向同一个文件（原文件 + 指向它的链接），重复处理没有意义，
    # 还会让摘要里的计数虚高，所以按解析后的真实路径去重。
    handled: set[Path] = set()
    for entry in entries:
        path = Path(entry.path)
        try:
            info = os.lstat(path)
        except OSError as exc:
            log.warning("无法读取 %s 的信息，跳过：%s", path, exc)
            stats.failed += 1
            continue
        if stat.S_ISLNK(info.st_mode):
            if not follow_links:
                # 给出链接目标：ssh 实际校验的是打开后的那个文件，只写“跳过”会让使用者
                # 不知道该去哪儿看权限，等于没说。
                try:
                    link_target = os.readlink(path)
                except OSError:
                    link_target = "（无法读取目标）"
                log.info("%s 是符号链接，跳过；它的目标是 %s，请确认该处的权限", path, link_target)
                stats.skipped += 1
                continue
            try:
                real = plain_path(path.resolve(strict=True))
                real_info = os.stat(real)
            except OSError as exc:
                log.warning("%s 是符号链接，但无法解析目标（%s），跳过", path, exc)
                stats.failed += 1
                continue
            # 只跟进指向普通文件的链接：目录链接会引入整棵树的处理范围，损坏或指向
            # 设备/管道的链接则没有“权限”可改。
            if not stat.S_ISREG(real_info.st_mode):
                log.warning("%s 指向 %s，不是普通文件，跳过", path, real)
                stats.skipped += 1
                continue
            if real in handled:
                log.info("%s 与已处理的条目指向同一文件 %s，跳过", path, real)
                stats.skipped += 1
                continue
            handled.add(real)
            log.info("%s 是符号链接，按 --follow-links 处理它的目标 %s", path, real)
            targets.append(Target(real, False))
            continue
        if stat.S_ISDIR(info.st_mode):
            log.info("%s 是子目录，不递归处理", path)
            stats.skipped += 1
            continue
        if not stat.S_ISREG(info.st_mode):
            log.info("%s 不是普通文件，跳过", path)
            stats.skipped += 1
            continue
        if path in handled:
            log.info("%s 与已处理的条目是同一个文件，跳过", path)
            stats.skipped += 1
            continue
        handled.add(path)
        targets.append(Target(path, False))
    return targets


def set_posix_mode(path: Path, observed: os.stat_result, desired: int) -> str | None:
    """把模式落到 path 上并回读确认，返回失败原因；成功返回 None。

    不按路径名直接 chmod：目录内容可能在“列出来”与“动手改”之间被换成指向别处的链接，
    按路径改会顺着链接改到 SSH 目录之外的文件。能拿到 O_NOFOLLOW 时改成对 fd 操作，
    链接根本打不开；打不开（例如文件本身 0000）或平台没有该标志时退回路径名改法，
    改完复核文件类型与 inode，两者都没变才认为改的是同一个对象。
    """
    try:
        now = os.lstat(path)
    except OSError as exc:
        return f"无法复核路径：{exc}"
    if stat.S_ISLNK(now.st_mode):
        return "路径已变成符号链接，拒绝改动"

    after = -1
    if getattr(os, "O_NOFOLLOW", 0):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            log.log(TRACE, "%s 无法按 nofollow 打开（%s），退回复核改法", path, exc)
        else:
            try:
                if stat.S_IFMT(os.fstat(fd).st_mode) != stat.S_IFMT(observed.st_mode):
                    return "打开后文件类型已变，放弃改动"
                os.fchmod(fd, desired)
                after = stat.S_IMODE(os.fstat(fd).st_mode)
            except OSError as exc:
                return f"设置权限失败：{exc}"
            finally:
                os.close(fd)

    if after < 0:
        try:
            os.chmod(path, desired)
            now = os.lstat(path)
        except OSError as exc:
            return f"设置权限失败：{exc}"
        if stat.S_ISLNK(now.st_mode) or (now.st_ino, now.st_dev) != (
            observed.st_ino,
            observed.st_dev,
        ):
            return "改动期间路径被替换，改的不是同一个文件"
        after = stat.S_IMODE(now.st_mode)

    if after != desired:
        return f"改动后仍是 {format_mode(after)}，未达到 {format_mode(desired)}"
    return None


def verify_posix(target: Target, stats: Stats, mode: str, pal: Palette) -> None:
    """读现状、必要时改，逐条报告。

    属主检查放在模式判断之前：文件不属于自己时 chmod 通常直接 EPERM，先说明原因比让使用者
    对着一条“权限不足”猜测有用；而且属主不是本人本身就是私钥被判不合法的条件之一。
    """
    try:
        before = os.lstat(target.path)
    except OSError as exc:
        log.error("无法读取 %s 的现状：%s", target.path, exc)
        stats.failed += 1
        return

    if before.st_uid != os.getuid():
        log.warning(
            "%s 的属主是 uid %d，不是当前用户（uid %d）；仅改模式可能不足以让它可用",
            target.path,
            before.st_uid,
            os.getuid(),
        )
        stats.warnings += 1

    desired = target.desired_mode
    actual = stat.S_IMODE(before.st_mode)
    if actual == desired:
        log.info("%s 已是 %s，跳过", target.path, format_mode(desired))
        stats.skipped += 1
        return

    if mode in (MODE_PREVIEW, MODE_CHECK):
        label = MODE_TEXT[mode][1]
        print(
            pal(
                f"  {label} {describe(target)}  当前 {format_mode(actual)}，应为 {format_mode(desired)}",
                "yellow",
            )
        )
        stats.changed += 1
        return

    error = set_posix_mode(target.path, before, desired)
    if error is not None:
        log.error("%s：%s", target.path, error)
        stats.failed += 1
        return

    print(pal(f"  已改 {describe(target)}  {format_mode(actual)} -> {format_mode(desired)}", "green"))
    stats.changed += 1


def find_icacls() -> str | None:
    """先在 PATH 里找，找不到再按 SystemRoot 下的既定位置回落。

    PATH 被裁剪掉 System32 的机器不少见，只靠 which 会让脚本在本来能用的系统上报“找不到”。
    """
    found = shutil.which("icacls")
    if found:
        return found
    system_root = os.environ.get("SystemRoot") or WINDOWS_SYSTEM_ROOT_FALLBACK
    candidate = Path(system_root) / "System32" / "icacls.exe"
    return str(candidate) if candidate.is_file() else None


def current_user_sid() -> str:
    """当前用户的 SID，取不到返回空串。

    授权主体只认 SID：账户名在本地账户、域账户与个人微软账户之间形态不同，与其猜一个
    也许能被 icacls 认出来的写法，不如直接失败并把原因说清楚。whoami 的列顺序随语言
    版本变化，所以倒着找第一个形如 S-1- 的单元格，而不是按位置取。
    """
    result = run_quiet(["whoami", *WINDOWS_WHOAMI_ARGS])
    if result is None or result.returncode != 0:
        return ""
    for line in reversed(result.stdout.strip().splitlines()):
        for cell in reversed(line.split(",")):
            value = cell.strip().strip('"')
            if value.upper().startswith("S-1-"):
                return value
    return ""


def grant_command(icacls: str, path: Path, principal: str, is_directory: bool) -> list[str]:
    """一条命令完成“断继承 + 授予白名单主体”。

    断继承与授权必须在同一次调用里：继承项一清而授权未落，文件会短暂处于无人可访问的
    状态，中途失败就等于把使用者锁在门外。icacls 允许在同一条命令里同时给出这两个开关。

    目录的授权项要带 (OI)(CI)：对象继承与容器继承，让之后新建的文件和子目录自动继承这套
    权限。少了这两个标志，目录本身干净了，但里面新生成的密钥仍会沿用父目录那套宽泛的
    默认 ACL，ssh 下次照样拒绝。
    """
    suffix = "(OI)(CI)(F)" if is_directory else "(F)"
    command = [icacls, str(path), "/inheritance:r", "/grant:r", f"{principal}:{suffix}"]
    for sid, _rights in WINDOWS_GRANTEES:
        command.append(f"{sid}:{suffix}")
    return command


def apply_windows(
    target: Target,
    stats: Stats,
    mode: str,
    pal: Palette,
    icacls: str,
    principal: str,
) -> None:
    """Windows 上按目标 ACL 施加改动，逐条报告。

    这里不判断“是否已经合规”：那需要读取并解析现有 ACL，而可用的解析途径都不可靠
    （icacls 输出是本地化显示名，PowerShell 又依赖模块自动加载，在 PSModulePath 被子进程
    污染时连 Get-Acl 都不可用）。代价是每次运行都会真的执行 icacls（对已合规的文件属于
    重复施加同样结果，无副作用），换来的是不会把“读不到”误报成“需要改”。
    """
    commands = [grant_command(icacls, target.path, principal, target.is_directory)]

    if mode in (MODE_PREVIEW, MODE_CHECK):
        label = MODE_TEXT[mode][1]
        print(pal(f"  {label} {describe(target)}", "yellow"))
        for command in commands:
            print(pal(f"      {subprocess.list2cmdline(command)}", "dim"))
        stats.changed += 1
        return

    for command in commands:
        result = run_quiet(command)
        if result is None or result.returncode != 0:
            detail = "（没有输出）" if result is None else (
                (result.stderr or "").strip() or (result.stdout or "").strip() or "（没有输出）"
            )
            code = "?" if result is None else result.returncode
            log.error("icacls 退出码 %s，%s 未完成：%s", code, target.path, detail)
            stats.failed += 1
            return
    print(pal(f"  已改 {describe(target)}", "green"))
    stats.changed += 1


def print_header(pal: Palette, ssh_dir: Path, platform_label: str, mode: str) -> None:
    print(pal("SSH 权限收紧", "bold", "cyan"))
    print(f"  目标目录：{ssh_dir}")
    print(f"  平台：{platform_label}")
    print(f"  模式：{MODE_TEXT[mode][0]}")
    print()


def print_summary(pal: Palette, stats: Stats, mode: str) -> None:
    print(pal("摘要", "bold"))
    print(
        f"  跳过 {stats.skipped}，{MODE_TEXT[mode][2]} {stats.changed}，"
        f"失败 {stats.failed}，告警 {stats.warnings}"
    )
    if stats.failed:
        print(pal("  有失败项，按上面的错误处理后重跑。", "red"))
    elif stats.changed and mode == MODE_APPLY:
        print(pal("  权限已按目标收紧。", "green"))
    elif stats.changed and mode == MODE_CHECK:
        print(pal("  当前状态不满足目标权限，需要处理。", "yellow"))
    elif stats.changed:
        print(pal("  加上 --apply 才会真正改写权限。", "dim"))
    else:
        print(pal("  没有需要改动的条目。", "dim"))
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_ssh_permissions.py",
        description="把 SSH 目录及其下文件的访问权限收紧到 0700/0600（默认只预演）",
    )
    parser.add_argument("--apply", action="store_true", help="真正改写权限")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只报告不合规项，不改动；忽略 --apply。仅 POSIX 可用",
    )
    parser.add_argument(
        "--ssh-dir", metavar="PATH", default=None, help=f"覆盖默认的 ~/{SSH_DIR_NAME}"
    )
    parser.add_argument(
        "--follow-links",
        action="store_true",
        help="符号链接不再跳过，改为处理它指向的真实文件（仅限指向普通文件的链接）",
    )
    parser.add_argument(
        "--log-level", default="info", metavar="LEVEL",
        help="日志级别：trace/debug/info/warn/error（默认 info）",
    )
    return parser


def configure_logging(level_name: str) -> None:
    # 先清掉旧 handler：模块被 import 后反复调用 main 时，handler 会越叠越多，
    # 同一条日志被打印好几遍，看起来像重复执行。
    log.handlers.clear()
    logging.addLevelName(TRACE, "TRACE")
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(LOG_LEVELS[level_name])


def main(argv: list[str] | None = None) -> int:
    # 输出被重定向时，Python 用系统默认编码写盘（中文 Windows 是 GBK），遇到编不出来的
    # 字符会抛 UnicodeEncodeError 打断流程。只把错误处理换成替换，编码仍由运行环境决定。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass

    parser = build_parser()
    args = parser.parse_args(argv)
    level_name = args.log_level.lower()
    if level_name not in LOG_LEVELS:
        parser.error(f"--log-level 只能是 trace/debug/info/warn/error，收到 {args.log_level!r}")
    configure_logging(level_name)

    mode = MODE_APPLY if args.apply else MODE_PREVIEW
    if args.check:
        if args.apply:
            log.info("同时给了 --apply 与 --check，以 --check 为准，不做任何改动")
        mode = MODE_CHECK

    try:
        ssh_dir = resolve_ssh_dir(args.ssh_dir)
    except HomeLookupError as exc:
        log.error("%s", exc)
        return EXIT_BAD_PATH

    if not ssh_dir.is_dir():
        log.error("SSH 目录不存在或不是目录：%s", ssh_dir)
        return EXIT_BAD_PATH

    if os.name not in ("nt", "posix"):
        log.error("不支持当前平台（os.name=%r），本脚本只处理 POSIX 与 Windows", os.name)
        return EXIT_UNSUPPORTED

    is_windows = os.name == "nt"
    if is_windows and mode == MODE_CHECK:
        # 不读 ACL 就无法判断现状，与其给出一个靠猜的结论，不如明确说明不支持。
        log.error("Windows 上不支持 --check：判断是否合规需要读取现有 ACL，本脚本刻意不做这件事")
        return EXIT_UNSUPPORTED

    icacls = find_icacls() if is_windows else None
    if is_windows and icacls is None:
        if mode == MODE_APPLY:
            log.error("在 PATH 与 System32 下都找不到 icacls，无法处理 Windows 的访问控制")
            return EXIT_UNSUPPORTED
        # 预演不改动任何东西，没有 icacls 也能把将要执行的命令摆出来，用工具名占位即可。
        log.warning("找不到 icacls，预演中的命令以工具名占位")

    pal = Palette(enable_ansi())
    print_header(pal, ssh_dir, "Windows" if is_windows else "POSIX", mode)

    stats = Stats()

    def gather() -> list[Target]:
        return collect_targets(
            ssh_dir, include_directory=True, stats=stats, follow_links=args.follow_links
        )

    try:
        targets = gather()
    except OSError as exc:
        # 只有真正要改动时才去修目录：预演与检查模式只报告，不动任何权限。
        if is_windows or mode != MODE_APPLY:
            log.error("无法读取目录 %s：%s", ssh_dir, exc)
            stats.failed += 1
            print_summary(pal, stats, mode)
            return EXIT_INCOMPLETE
        log.warning(
            "目录 %s 当前不可读（%s），先把它恢复为 %s 再继续",
            ssh_dir,
            exc,
            format_mode(POSIX_DIR_MODE),
        )
        try:
            os.chmod(ssh_dir, POSIX_DIR_MODE)
            targets = gather()
        except OSError as repair_error:
            log.error("恢复目录 %s 的权限失败：%s", ssh_dir, repair_error)
            stats.failed += 1
            print_summary(pal, stats, mode)
            return EXIT_INCOMPLETE

    if is_windows:
        sid = current_user_sid()
        if not sid:
            log.error("无法从 whoami 取得当前用户的 SID，不能生成授权项")
            return EXIT_UNSUPPORTED
        principal = "*" + sid
        for target in targets:
            apply_windows(target, stats, mode, pal, icacls or "icacls", principal)
    else:
        for target in targets:
            verify_posix(target, stats, mode, pal)

    print_summary(pal, stats, mode)

    if mode == MODE_CHECK:
        return EXIT_INCOMPLETE if (stats.changed or stats.failed or stats.warnings) else EXIT_OK
    return EXIT_INCOMPLETE if stats.failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())