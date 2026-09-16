"""只读红线守卫（infra.fs_lock，对应架构设计 9.4 铁律一）。

职责：
  * :func:`input_readonly` —— 上下文管理器，包裹所有对**输入**（Excel / 图片目录）的访问，
    进入时断言路径可读且不是本工具的输出目录，退出时校验输入未被改动。
  * :func:`assert_readonly` —— 断言路径不是"被禁止写入"的目标。
  * :func:`assert_within` —— 断言目标路径落在允许的根目录内（产物分流强制）。
  * Windows ``ReadOnly`` 文件属性探测。

**设计原则**：本工具**绝不写入**用户选定的输入路径。任何写操作前都必须调用
:func:`assert_readonly`（写目标不得是受保护输入）或 :func:`assert_within`
（写目标必须在允许的输出根内），否则抛 :class:`OutputPathViolation`。
"""

from __future__ import annotations

import contextlib
import os
import stat as stat_module
from collections.abc import Iterable, Iterator
from pathlib import Path

from infra.encoding import normalize_path
from infra.errors import OutputPathViolation, PathNotAccessibleError

__all__ = [
    "is_readonly_file",
    "assert_readonly",
    "assert_within",
    "input_readonly",
]


def is_readonly_file(path: str | os.PathLike[str]) -> bool:
    """探测文件是否带 Windows ``ReadOnly`` 属性。

    Args:
        path: 文件路径。

    Returns:
        ``True`` 表示文件带只读属性（或不可写）；不存在返回 ``False``。
    """
    p = normalize_path(path)
    try:
        file_stat = os.stat(p)
    except OSError:
        return False
    # stat.S_IWRITE 是 Windows 只读属性的映射
    return not bool(file_stat.st_mode & stat_module.S_IWRITE)


def assert_readonly(
    target: str | os.PathLike[str],
    protected_roots: Iterable[str | os.PathLike[str]],
) -> None:
    """断言 ``target`` 不落在任何受保护输入根内（防误写输入）。

    Args:
        target: 期望写入的目标路径（文件或目录）。
        protected_roots: 受保护根列表（用户选定的 Excel 所在目录、图片根等）。

    Raises:
        OutputPathViolation: 目标位于受保护根内（违反「原始输入只读」铁律）。
    """
    tgt = normalize_path(target).resolve(strict=False)
    for root in protected_roots:
        if root is None or str(root).strip() == "":
            continue
        root_path = normalize_path(root).resolve(strict=False)
        # 文件路径先取父目录再比较
        candidate = tgt if tgt.is_dir() else tgt.parent
        if candidate == root_path or root_path in candidate.parents:
            raise OutputPathViolation(
                f"refuse to write into protected input: {tgt} (root={root_path})",
                path=str(tgt),
            )


def assert_within(target: str | os.PathLike[str], allowed_root: str | os.PathLike[str]) -> None:
    """断言 ``target`` 落在 ``allowed_root`` 内（产物分流强制）。

    用于写「成果产出」时校验目标确在成果根内，写「过程产出」时校验在过程根内。

    Args:
        target: 期望写入的目标路径。
        allowed_root: 唯一允许的根目录。

    Raises:
        OutputPathViolation: 目标越界。
    """
    tgt = normalize_path(target).resolve(strict=False)
    root_path = normalize_path(allowed_root).resolve(strict=False)
    candidate = tgt if tgt.is_dir() else tgt.parent
    if candidate != root_path and root_path not in candidate.parents:
        raise OutputPathViolation(
            f"write target out of allowed root: {tgt} (allowed={root_path})",
            path=str(tgt),
        )


@contextlib.contextmanager
def input_readonly(
    path: str | os.PathLike[str],
    *,
    expect_exists: bool = True,
) -> Iterator[Path]:
    """只读访问输入的上下文管理器。

    进入时：
      1. 规范化路径；
      2. 若 ``expect_exists``，断言存在且可读（不可读抛 :class:`PathNotAccessibleError`）；
      3. 记录进入时的 ``(mtime, size)`` 快照。

    退出时：
      4. 重新读取 ``(mtime, size)``；若发生变化，抛 :class:`OutputPathViolation`
         （说明有代码违规改了输入 —— 严格守卫，宁可报错也不静默放过）。

    Args:
        path: 输入路径（Excel 文件或图片目录）。
        expect_exists: 是否要求路径必须存在（默认 ``True``）。

    Yields:
        规范化后的 :class:`pathlib.Path`。

    Raises:
        PathNotAccessibleError: 路径不存在 / 不可读。
        OutputPathViolation: 退出时检测到输入被改动。
    """
    p = normalize_path(path)

    if expect_exists:
        if not p.exists():
            raise PathNotAccessibleError(
                f"input path not found: {p}", path=str(p)
            )
        if not os.access(p, os.R_OK):
            raise PathNotAccessibleError(
                f"input path not readable: {p}", path=str(p)
            )

    snapshot_before = _snapshot(p)
    try:
        yield p
    finally:
        snapshot_after = _snapshot(p)
        if snapshot_before is not None and snapshot_after is not None:
            if snapshot_before != snapshot_after:
                raise OutputPathViolation(
                    f"input was modified during read-only access: {p}",
                    path=str(p),
                )


def _snapshot(path: Path) -> tuple[float, int] | None:
    """取路径的 ``(mtime, size)`` 快照；失败返回 ``None``。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)
