#!/usr/bin/env python3
"""清理微信接收文件时产生的重复硬链接路径

微信接收同名文件时会在同一目录里为它建立硬链接，于是同一个 inode 上挂着
X.pdf 与 X(1).pdf 两条路径。路径多了以后，目录看着乱，同步、检索、备份工具
也会把同一份数据数很多遍。

用法:
  python wechat_hardlink_cleaner.py ROOT            # 只打印计划，不动文件
  python wechat_hardlink_cleaner.py ROOT --apply    # 按计划删除多余路径
  python wechat_hardlink_cleaner.py                 # 预演当前目录
  python wechat_hardlink_cleaner.py ROOT --log-level debug

安全边界（这几条是刻意的限制，不是功能缺失）:
  - 只处理同一目录内、同一 (st_dev, st_ino)、st_nlink >= 2 的文件组
  - 只删名字里带半角括号数字后缀的路径；没有编号的原名一律保留
  - 删除前重新 stat 确认 inode 未变；绝不 rename、不删目录、不跟随符号链接
  - Windows 上待删文件带只读属性时，先清除只读再删，删完把保留项恢复为只读
  - 不碰 git：遍历时跳过 .git 目录，也不调用任何 git 命令

退出码:
  0  正常结束（预演、诊断、执行成功都算正常）
  1  根目录不存在或不是目录
  2  有路径在执行时未能删除（常见原因是文件被占用或权限不足，处理后重跑即可）
"""

import argparse
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass, field

# 末尾的“半角括号 + 纯数字”。只认半角：微信（以及多数同步工具）生成的是半角括号，
# 全角括号往往是用户自己起的名字，误删代价比漏删大得多。
SUFFIX_RE = re.compile(r"\((\d+)\)$")

# Windows 控制台默认把 ANSI 序列当普通字符打出来，必须先打开这个模式位。
VT_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11

TRACE = 5
LOG_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

log = logging.getLogger("cleaner")


class Palette:
    """ANSI 着色开关集中在一处，关掉时所有调用退化为原文，调用点不必写条件。"""

    CODES = {
        "reset": "\x1b[0m",
        "bold": "\x1b[1m",
        "dim": "\x1b[2m",
        "red": "\x1b[31m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "blue": "\x1b[34m",
        "cyan": "\x1b[36m",
    }

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        return "".join(self.CODES[s] for s in styles) + text + self.CODES["reset"]


def enable_ansi() -> bool:
    """决定能不能安全地输出颜色；判断失败一律当作不能，避免满屏乱码。"""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_handle = k32.GetStdHandle
        get_handle.restype = wintypes.HANDLE
        get_handle.argtypes = [wintypes.DWORD]
        get_mode = k32.GetConsoleMode
        get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        set_mode = k32.SetConsoleMode
        set_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        handle = get_handle(wintypes.DWORD(STD_OUTPUT_HANDLE & 0xFFFFFFFF))
        mode = wintypes.DWORD()
        if not get_mode(handle, ctypes.byref(mode)):
            return False
        if mode.value & VT_PROCESSING:
            return True
        return bool(set_mode(handle, wintypes.DWORD(mode.value | VT_PROCESSING)))
    except Exception:
        # 老系统、被重定向的句柄、受限的容器里都可能失败；没有颜色也能用。
        return False


@dataclass(frozen=True)
class Entry:
    """一个普通文件路径及其扫描时的身份信息。"""

    path: str
    name: str
    dev: int
    ino: int
    nlink: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class NameForm:
    """文件名的一种“剥离了若干层编号”的写法。

    level 为剥离层数，0 表示文件名本身；number 是这一层剥掉的编号，
    level 0 时取文件名末尾的编号（没有则为 None），用来在没有原名时比大小。
    """

    name: str
    level: int
    number: int | None


def _suffix_layers(base: str) -> list[tuple[str, int]]:
    """从 base 尾部逐层剥掉 (数字)，返回每层剥离后的前缀与被剥掉的编号。"""
    layers: list[tuple[str, int]] = []
    cur = base
    while True:
        # 先去掉尾部空格再匹配：出现“X(1) ”这种结尾时仍要能认出来
        trimmed = cur.rstrip(" ")
        match = SUFFIX_RE.search(trimmed)
        if match is None:
            return layers
        prefix = trimmed[: match.start()]
        layers.append((prefix, int(match.group(1))))
        cur = prefix


def _trailing_number(base: str) -> int | None:
    match = SUFFIX_RE.search(base.rstrip(" "))
    return int(match.group(1)) if match else None


def name_forms(name: str) -> list[NameForm]:
    """列出文件名所有“可能是原名”的写法，按层数从少到多。"""
    base, ext = os.path.splitext(name)
    forms = [NameForm(name, 0, _trailing_number(base))]
    for level, (prefix, number) in enumerate(_suffix_layers(base), start=1):
        # 括号前的空格既可能是原名的一部分，也可能是微信自己加的；
        # 两种都当候选，最后只认组内真实存在的那个，避免凭猜测改名。
        variants = [prefix.rstrip(" ")]
        if prefix != variants[0]:
            variants.append(prefix)
        for variant in variants:
            candidate = variant + ext
            if candidate and candidate not in (".", ".."):
                forms.append(NameForm(candidate, level, number))
    return forms


def suffix_depth(name: str) -> int:
    base, _ = os.path.splitext(name)
    return len(_suffix_layers(base))


def root_key(name: str) -> str:
    """把同一份数据的各种写法折算成一个根名，用来发现“同 inode 却毫不相干”的组。

    只有剥过至少一层才去掉前缀末尾空格：那个空格本来就在括号前面，属于编号写法
    的一部分；没有编号的文件名保留原样，否则 X .pdf 会被误认成 X.pdf 的写法。
    """
    base, ext = os.path.splitext(name)
    layers = _suffix_layers(base)
    if not layers:
        return base + ext
    return layers[-1][0].rstrip(" ") + ext


def _rank(form: NameForm, target: Entry) -> tuple:
    """比较候选写法的优先级：层数少的优先，同层编号小的优先，其余只为结果稳定。"""
    return (
        form.level,
        -1 if form.number is None else form.number,
        len(target.name),
        target.name,
    )


@dataclass
class GroupPlan:
    directory: str
    keeper: Entry
    victims: list[Entry] = field(default_factory=list)


@dataclass
class Resolution:
    keeper: Entry | None = None
    victims: list[Entry] = field(default_factory=list)
    skip_reason: str = ""


def resolve_group(entries: list[Entry]) -> Resolution:
    roots = {root_key(e.name) for e in entries}
    if len(roots) > 1:
        # 同 inode 但根名不同（a.pdf 与 b.pdf），说明文件名和数据对不上号，
        # 此时任何猜测都可能删掉用户真正想引用的那个名字。
        return Resolution(skip_reason="根名不一致：" + "、".join(sorted(roots)))

    by_name = {e.name: e for e in entries}
    best: tuple[tuple, Entry] | None = None
    for entry in entries:
        for form in name_forms(entry.name):
            target = by_name.get(form.name)
            if target is None:
                # 候选写法必须是组内真实存在的同 inode 路径，没出现就不算数
                continue
            key = _rank(form, target)
            if best is None or key < best[0]:
                best = (key, target)

    if best is None:
        return Resolution(skip_reason="组内找不到可识别的原名写法")

    keeper = best[1]
    # 双重保险：带编号的才可能被删。上面已经排除了根名不一致，这里再挡一次，
    # 是因为“删掉一个没有编号的名字”属于不可挽回的操作。
    victims = [e for e in entries if e.name != keeper.name and suffix_depth(e.name) > 0]
    return Resolution(keeper=keeper, victims=victims)


@dataclass
class Stats:
    directories: int = 0
    regular_files: int = 0
    symlinks: int = 0
    groups: int = 0
    extra_paths: int = 0
    resolved_groups: int = 0
    planned: int = 0
    skipped_multiple_roots: int = 0
    skipped_unresolved: int = 0
    zero_ino: int = 0
    singleton_links: int = 0
    deleted: int = 0
    failed: int = 0


def scan_directory(directory: str, stats: Stats) -> dict[tuple[int, int], list[Entry]]:
    groups: dict[tuple[int, int], list[Entry]] = {}
    zero_ino = 0
    with os.scandir(directory) as it:
        for item in it:
            # 这里必须用 os.stat 而不是 item.stat()：Windows 上 DirEntry.stat()
            # 返回的 st_ino、st_dev、st_nlink 恒为 0，用它扫描会让所有文件都被跳过，
            # 程序看起来“没发现硬链接”，在 Windows 上等于失效。
            try:
                info = os.stat(item.path, follow_symlinks=False)
            except OSError as exc:
                log.debug("无法读取 %s 的信息：%s", item.path, exc)
                continue
            if not stat.S_ISREG(info.st_mode):
                # 目录由 os.walk 负责递归；符号链接与设备文件不在处理范围内
                if stat.S_ISLNK(info.st_mode):
                    stats.symlinks += 1
                continue
            stats.regular_files += 1
            if info.st_nlink < 2:
                continue
            if info.st_ino == 0:
                # FAT32、部分网络盘拿不到文件索引号，此时所有文件 ino 都是 0，
                # 按 ino 分组会把整目录当成一份数据，必须直接放弃。
                zero_ino += 1
                continue
            key = (info.st_dev, info.st_ino)
            groups.setdefault(key, []).append(
                Entry(
                    item.path,
                    item.name,
                    info.st_dev,
                    info.st_ino,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                )
            )
    if zero_ino:
        stats.zero_ino += zero_ino
        log.warning("%s：%d 个文件拿不到 inode 编号（文件系统不支持？），已跳过", directory, zero_ino)
    paired: dict[tuple[int, int], list[Entry]] = {}
    for key, entries in groups.items():
        if len(entries) >= 2 and entries[0].nlink >= 2:
            paired[key] = entries
        else:
            # 链接数大于 1、同目录里却只有一条路径，说明其余链接建在别处。
            # 统计出来是为了让用户能判断微信到底有没有跨目录建链接。
            stats.singleton_links += 1
    return paired


def _warn_walk_error(exc: OSError) -> None:
    # 权限不足、目录被并发删除、网络盘中转失败都会走到这里。悄悄跳过会让用户
    # 以为整个目录树都扫过了，所以按 warn 记下来。
    log.warning("无法读取目录 %s：%s", exc.filename or "（未知路径）", exc)


def plan_tree(root: str, stats: Stats) -> list[GroupPlan]:
    plans: list[GroupPlan] = []
    for dirpath, dirnames, _ in os.walk(root, onerror=_warn_walk_error):
        # .git 目录里的硬链接去重没有意义，也不该让脚本有机会碰仓库元数据
        dirnames[:] = [d for d in dirnames if d != ".git"]
        stats.directories += 1
        try:
            groups = scan_directory(dirpath, stats)
        except OSError as exc:
            log.warning("无法读取目录 %s：%s", dirpath, exc)
            continue
        log.log(TRACE, "%s：%d 组候选硬链接", dirpath, len(groups))
        for entries in groups.values():
            stats.groups += 1
            stats.extra_paths += len(entries) - 1
            resolution = resolve_group(entries)
            if resolution.keeper is None:
                if "根名" in resolution.skip_reason:
                    stats.skipped_multiple_roots += 1
                else:
                    stats.skipped_unresolved += 1
                log.warning(
                    "%s：跳过一组同名 inode 文件（%s）",
                    dirpath,
                    resolution.skip_reason,
                )
                continue
            if not resolution.victims:
                # 认出了保留项却没有可删的路径：只在 trace 里留痕，不占用计划输出。
                # （根名一致时每个成员都带编号才能进到这里，实际几乎不会发生。）
                log.log(TRACE, "%s：组内无可删路径，保留 %s", dirpath, resolution.keeper.name)
                continue
            stats.resolved_groups += 1
            stats.planned += len(resolution.victims)
            plans.append(GroupPlan(dirpath, resolution.keeper, resolution.victims))
    return plans


def _unchanged(entry: Entry, info: os.stat_result) -> bool:
    """判断路径是否还是扫描时那一份数据。

    inode 编号会被文件系统重用：旧文件删掉后，新文件完全可能拿到同一个号。
    因此除了 (st_dev, st_ino)，再比对大小与修改时间，三者都一致才认。
    """
    return (
        stat.S_ISREG(info.st_mode)
        and (info.st_dev, info.st_ino) == (entry.dev, entry.ino)
        and info.st_size == entry.size
        and info.st_mtime_ns == entry.mtime_ns
    )


def _is_readonly(info: os.stat_result) -> bool:
    """Windows 上 DeleteFile 对带只读属性的文件返回 ERROR_ACCESS_DENIED。"""
    return not (info.st_mode & stat.S_IWRITE)


def apply_plan(plans: list[GroupPlan], stats: Stats) -> None:
    for plan in plans:
        # 硬链接共享同一个文件记录，只读属性也是共享的。为了让删除通过而清掉只读，
        # 会把保留项一起变成可写，所以先记下原状态，这一组处理完再恢复。
        keeper_was_readonly: bool | None = None
        for victim in plan.victims:
            # 计划与执行之间可能隔着几秒，用户也可能同时开着微信在下载。
            # 每次删除前重新核对，是为了让“删错文件”在机制上不可能发生。
            try:
                current = os.lstat(victim.path)
            except OSError as exc:
                log.warning("删除前无法核对 %s：%s，跳过", victim.path, exc)
                stats.failed += 1
                continue
            if not _unchanged(victim, current):
                log.warning("%s 在执行前发生变化，跳过", victim.path)
                stats.failed += 1
                continue
            # 保留项也必须还在：否则删完最后一个多余路径，这份数据就没有任何
            # 路径指向它了，那是真正的数据丢失。
            try:
                keeper_info = os.lstat(plan.keeper.path)
            except OSError as exc:
                log.warning("保留项 %s 已不可用（%s），跳过整组", plan.keeper.path, exc)
                stats.failed += 1
                break
            if not _unchanged(plan.keeper, keeper_info):
                log.warning("保留项 %s 的内容或 inode 已变化，跳过整组", plan.keeper.path)
                stats.failed += 1
                break
            if os.name == "nt" and _is_readonly(current):
                if keeper_was_readonly is None:
                    keeper_was_readonly = _is_readonly(keeper_info)
                try:
                    os.chmod(victim.path, current.st_mode | stat.S_IWRITE)
                except OSError as exc:
                    log.error("无法清除只读属性 %s：%s", victim.path, exc)
                    stats.failed += 1
                    continue
                log.info("已清除只读属性以便删除 %s", victim.path)
            try:
                os.unlink(victim.path)
            except OSError as exc:
                # Windows 上文件被占用（微信刚接收完、杀毒软件正在扫）时会走到这里，
                # 只记录失败，让用户关掉占用程序后重跑。
                log.error("删除失败 %s：%s", victim.path, exc)
                stats.failed += 1
            else:
                stats.deleted += 1
                log.debug("已删除 %s", victim.path)
        if keeper_was_readonly:
            try:
                info = os.lstat(plan.keeper.path)
                os.chmod(plan.keeper.path, info.st_mode & ~stat.S_IWRITE)
                log.info("已把保留项恢复为只读 %s", plan.keeper.path)
            except OSError as exc:
                log.warning("保留项 %s 恢复只读失败：%s", plan.keeper.path, exc)


def print_header(pal: Palette, root: str, apply: bool) -> None:
    print(pal("微信硬链接清理", "bold", "cyan"))
    print(f"  根目录：{root}")
    if apply:
        print(pal("  模式：执行（会删除多余路径）", "yellow"))
    else:
        print("  模式：预演（只打印计划，不改动任何文件）")
    print()


def _victim_order(entry: Entry) -> tuple[int, int, str]:
    """按编号升序排列待删项：X(2) 排在 X(10) 前面，符合人读编号的习惯。"""
    number = _trailing_number(entry.name)
    return (0 if number is None else 1, -1 if number is None else number, entry.name)


def print_plan(pal: Palette, root: str, plans: list[GroupPlan]) -> None:
    if not plans:
        print(pal("没有需要删除的重复路径。", "dim"))
        print()
        return
    # 按目录聚合成块：同一目录常常有好几组，逐组重印目录名会让人误以为它们在不同位置
    by_dir: dict[str, list[GroupPlan]] = {}
    for plan in plans:
        by_dir.setdefault(plan.directory, []).append(plan)
    for directory, group_plans in by_dir.items():
        print(pal(f"目录 {os.path.relpath(directory, root)}", "bold"))
        for index, plan in enumerate(group_plans):
            if index:
                print()
            # 组里没有原名时，说明一句为什么挑这一条，否则读起来像随手选的
            note = "（无原名，保留编号最小）" if suffix_depth(plan.keeper.name) > 0 else ""
            print(f"  保留  {pal(plan.keeper.name, 'green')}{note}")
            for victim in sorted(plan.victims, key=_victim_order):
                print(f"  删除  {pal(victim.name, 'yellow')}")
        print()


def print_summary(pal: Palette, stats: Stats, apply: bool) -> None:
    print(pal("摘要", "bold"))
    print(f"  扫描目录 {stats.directories} 个，普通文件 {stats.regular_files} 个")
    print(
        f"  硬链接组 {stats.groups} 组，组内多余路径 {stats.extra_paths} 条，"
        f"可处理 {stats.resolved_groups} 组"
    )
    if stats.skipped_multiple_roots or stats.skipped_unresolved:
        print(
            f"  跳过 {stats.skipped_multiple_roots + stats.skipped_unresolved} 组"
            f"（根名不一致 {stats.skipped_multiple_roots}，无法识别 {stats.skipped_unresolved}）"
        )
    if stats.zero_ino:
        print(f"  因缺少 inode 编号忽略文件 {stats.zero_ino} 个")
    if stats.singleton_links:
        print(
            f"  另有 {stats.singleton_links} 个文件链接数大于 1，但同目录内只有一条路径"
            "（链接建在别处，未处理）"
        )
    if apply:
        print(f"  已删除 {stats.deleted} 条路径，失败 {stats.failed} 条")
    else:
        print(f"  计划删除 {stats.planned} 条路径（加上 --apply 才会真正执行）")
    print()
    # 这条容易被误解成“清理能腾空间”，必须写清楚，否则用户会拿它当磁盘清理工具。
    print(pal("  注意：删除硬链接不释放额外磁盘空间，只减少重复路径。", "yellow"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat_hardlink_cleaner.py",
        description="清理微信接收目录里由硬链接产生的重复路径（默认只打印计划）",
    )
    parser.add_argument(
        "root",
        metavar="ROOT",
        nargs="?",
        default=".",
        help="要扫描的根目录（默认当前目录）",
    )
    parser.add_argument("--apply", action="store_true", help="真正删除多余路径")
    parser.add_argument(
        "--log-level",
        default="info",
        metavar="LEVEL",
        help="日志级别：trace/debug/info/warn/error（默认 info）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # 输出被重定向到文件时，Python 用系统默认编码（简体中文 Windows 是 GBK）写盘，
    # 遇到编不出来的字符会抛 UnicodeEncodeError 把整个清理打断。只把错误处理换成
    # 替换，不动编码：编码仍由运行环境决定，管道与重定向的行为不变。
    # stderr 一并处理：日志里带着真实文件名，生僻字同样会让 logging 报错。
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
        # warning 是 logging 的正式名称，写在这里但不放进提示，避免提示本身显得罗嗦
        parser.error(f"--log-level 只能是 trace/debug/info/warn/error，收到 {args.log_level!r}")
    logging.addLevelName(TRACE, "TRACE")
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(LOG_LEVELS[level_name])

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        log.error("根目录不存在或不是目录：%s", root)
        return 1

    pal = Palette(enable_ansi())
    print_header(pal, root, args.apply)

    stats = Stats()
    plans = plan_tree(root, stats)
    print_plan(pal, root, plans)
    if args.apply:
        apply_plan(plans, stats)
    print_summary(pal, stats, apply=args.apply)
    # 有失败时用非零退出码，方便放进脚本里判断是否需要关掉微信再跑一次
    return 2 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())