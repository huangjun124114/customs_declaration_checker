"""编码与路径安全读写封装（infra.encoding）。

职责：
  1. **启动即强制 UTF-8**：设置 ``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8``
     （对应 SOP 陷阱 #4/#5；中文参数**禁止经命令行传递**，一律进程内赋值）。
  2. **中文 / UNC 路径安全读写**：所有文本读写显式指定编码，CSV 默认带 BOM
     （``utf-8-sig``）防止 Excel 打开乱码。
  3. **路径规范化**：清洗 Qt ``QFileDialog`` 可能返回的 ``file:///`` 前缀、
     统一 UNC 反斜杠、去掉尾随分隔符（供断点指纹比对使用）。

本模块**不得** import 任何上层模块。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "force_utf8_encoding",
    "normalize_path",
    "normalize_path_str",
    "normalize_path_text",
    "read_text",
    "write_text",
    "read_json",
    "write_json",
    "file_sha256",
    "safe_listdir",
]


def force_utf8_encoding() -> None:
    """强制进程使用 UTF-8 编码。

    必须在 ``main.py`` 的**最早期**调用（早于任何文件/网络/子进程 IO）。
    已设置的值不会被覆盖，避免违反用户显式配置。

    行为：
      * ``PYTHONUTF8=1``         —— 开启 Python UTF-8 模式（PEP 540）。
      * ``PYTHONIOENCODING=utf-8`` —— 强制 stdin/stdout/stderr 使用 UTF-8。
      * **重配已打开的 std 流**（见 :func:`_reconfigure_std_streams`）——
        环境变量只对「之后创建」的解释器/流生效，对**当前进程已建好的**
        std 流无效，必须显式 ``reconfigure``。
    """
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _reconfigure_std_streams()


def _reconfigure_std_streams() -> None:
    """把**当前进程已打开的** stdin/stdout/stderr 重配为 UTF-8。

    为什么必须做：``PYTHONIOENCODING`` 只影响之后创建的流。进程启动时
    std 流已按系统 ANSI 代码页建好 —— 简体中文 Windows 上是 **GBK** ——
    此时 ``print("✅ 校验合格")`` 会抛 ``UnicodeEncodeError``。

    打包成窗口程序（``console=False``）后更易踩到：实测 exe 的 ``--self-test``
    就因打印 ``✅`` 而失败（断言全过，却返回退出码 1，误导排错方向）。

    ``errors="replace"``：控制台输出**绝不能**因编码问题崩掉程序，
    个别字符降级成 ``?`` 远好过整个 CLI 命令失败。
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            # 窗口模式（pythonw / PyInstaller console=False）下无控制台，
            # 标准流可能为 None；此时 print 会静默丢弃，属预期。
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 流已被重定向成非文本对象（如 pytest 的 capture）时忽略
            pass


def normalize_path(path: str | os.PathLike[str]) -> Path:
    """把任意用户/ Qt 传入的路径字符串规范化为 :class:`pathlib.Path`。

    处理内容：
      * 去除 Qt ``QFileDialog`` 可能返回的 ``file:///`` 前缀；
      * Windows 盘符路径 ``/D:/x`` 修正为 ``D:/x``；
      * 统一为正斜杠交由 :class:`pathlib.Path` 处理（``Path`` 会自动适配平台）。

    Args:
        path: 原始路径字符串或 ``Path``。

    Returns:
        规范化后的 :class:`pathlib.Path`。空字符串返回 ``Path('.')``。
    """
    if isinstance(path, Path):
        raw = str(path)
    else:
        raw = "" if path is None else str(path)

    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return Path(".")

    # Qt 在部分平台会返回 file:///D:/a/b 形式
    if raw.lower().startswith("file:///"):
        raw = raw[len("file:///") :]
    elif raw.lower().startswith("file://"):
        raw = raw[len("file://") :]

    # 修正 "/D:/path" → "D:/path"
    if len(raw) >= 3 and raw[0] == "/" and raw[1].isalpha() and raw[2] == ":":
        raw = raw[1:]

    return Path(raw)


def normalize_path_text(path: str | os.PathLike[str]) -> str:
    """规范化路径并返回字符串，**但不经 pathlib 重写**（关键：UNC 安全）。

    与 :func:`normalize_path` 的区别：``pathlib.Path("\\\\\\\\h\\\\s")`` 会把 UNC 根
    规范化成 ``\\\\\\\\h\\\\s\\\\``（补尾反斜杠），这对"用户原样输入的路径"是有损的。
    本函数只做「去引号 / 去 file:/// 前缀 / 去尾随分隔符」等**无副作用**清洗，
    保留用户输入的原始形态，适用于配置存储与展示。

    Args:
        path: 原始路径字符串或 ``Path``。

    Returns:
        清洗后的路径字符串（保留原分隔符风格与 UNC 形态）。
    """
    raw = str(path) if path is not None else ""
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return ""

    lowered = raw.lower()
    if lowered.startswith("file:///"):
        raw = raw[len("file:///") :]
    elif lowered.startswith("file://"):
        raw = raw[len("file://") :]

    # 修正 "/D:/path" → "D:/path"
    if len(raw) >= 3 and raw[0] == "/" and raw[1].isalpha() and raw[2] == ":":
        raw = raw[1:]

    return raw


def normalize_path_str(path: str | os.PathLike[str]) -> str:
    """规范化路径并返回**用于指纹比对**的稳定字符串。

    规则（对应架构设计 12.A.3「share_root 规范化」）：
      * 统一分隔符为 ``\\``（Windows 原生，UNC 亦适用）；
      * 去除尾随分隔符（根目录 ``C:\\`` 除外）；
      * 盘符统一小写；
      * 去掉 ``file:///`` 前缀。

    Args:
        path: 原始路径。

    Returns:
        规范化后的字符串（比对用）。
    """
    p = normalize_path(path)
    text = str(p).replace("/", "\\")

    # 去除尾随反斜杠（保留盘符根 "C:\" 与 UNC 根 "\\host\share\"）
    while len(text) > 3 and text.endswith("\\"):
        text = text[:-1]

    # 盘符统一小写（C: → c:）；UNC 路径（\\ 开头）不做盘符处理
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        text = text[0].lower() + text[1:]

    return text


def read_text(path: str | os.PathLike[str], encoding: str = "utf-8") -> str:
    """读取文本文件（显式编码，兼容中文 / UNC 路径）。

    Args:
        path: 文件路径。
        encoding: 编码，默认 ``utf-8``。

    Returns:
        文件内容字符串。

    Raises:
        FileNotFoundError: 路径不存在。
        PermissionError: 无读权限。
        UnicodeDecodeError: 编码不匹配（调用方可传 ``utf-8-sig`` 兜底）。
    """
    p = normalize_path(path)
    with open(p, encoding=encoding) as fh:
        return fh.read()


def write_text(
    path: str | os.PathLike[str],
    content: str,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Path:
    """写入文本文件（自动创建父目录）。

    Args:
        path: 目标文件路径。
        content: 文本内容。
        encoding: 编码，默认 ``utf-8``；导出 CSV 时建议 ``utf-8-sig``（带 BOM 防 Excel 乱码）。
        newline: 换行符策略，``""`` 可避免 Windows 下 ``\\r\\n`` 重复。

    Returns:
        实际写入的 :class:`pathlib.Path`。
    """
    p = normalize_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding=encoding, newline=newline) as fh:
        fh.write(content)
    return p


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    """读取 JSON 文件（UTF-8）。

    Args:
        path: JSON 文件路径。
        default: 文件不存在或解析失败时返回的默认值；为 ``None`` 时抛异常。

    Returns:
        解析后的 Python 对象。

    Raises:
        FileNotFoundError: 文件不存在且 ``default is None``。
        json.JSONDecodeError: JSON 非法且 ``default is None``。
    """
    p = normalize_path(path)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        if default is not None:
            return default
        raise


def write_json(path: str | os.PathLike[str], data: Any, indent: int = 2) -> Path:
    """写入 JSON 文件（UTF-8、不转义中文、自动建父目录）。

    Args:
        path: 目标路径。
        data: 可 JSON 序列化对象。
        indent: 缩进空格数。

    Returns:
        实际写入的 :class:`pathlib.Path`。
    """
    p = normalize_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=indent)
    return p


def file_sha256(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 sha256（大文件安全，UNC 亦可）。

    用于断点指纹（架构设计 12.A.3 / D.1）：Excel 文件字节哈希。

    Args:
        path: 文件路径。
        chunk_size: 分块大小（默认 1 MiB）。

    Returns:
        十六进制 sha256 字符串；读取失败返回空字符串（调用方回退路径比对）。
    """
    p = normalize_path(path)
    digest = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(chunk_size), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def safe_listdir(path: str | os.PathLike[str]) -> list[str]:
    """安全列出目录条目（不抛异常）。

    UNC 不可达 / 权限不足 / 路径不存在时返回空列表，由调用方决定如何提示。

    Args:
        path: 目录路径。

    Returns:
        条目名称列表（非完整路径）；失败返回 ``[]``。
    """
    p = normalize_path(path)
    try:
        return os.listdir(p)
    except OSError:
        return []
