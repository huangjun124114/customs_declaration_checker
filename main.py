"""程序唯一入口（main.py）。

启动顺序（**严格按架构设计 12.B.3 + 12.E 要求**）：

  1. **编码预置** —— 强制 ``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8``
     （必须早于任何 IO；SOP 陷阱 #3/#4/#5）。
  2. **初始化日志** —— 统一日志系统（滚动文件 + 控制台 + 信号 bus）。
  3. **加载规则** —— ``RuleRepository.load_all()`` 一次性冻结快照（v1.1 C.3）。
  4. **DPI 策略** —— ``QApplication.setHighDpiScaleFactorRoundingPolicy(PassThrough)``
     （Win10 125%/150% 缩放不失真，12.B.3 注意事项 #4）。
  5. **创建 QApplication + MainWindow**，进入事件循环。

隐藏 CLI 参数（Q8，不作为用户主路径）：
  * ``--self-test``    跑自检（规则加载 + 模型导入）后退出；
  * ``--check-env``    调用 ``tools/check_env.py`` 做环境体检后退出。

**中文参数禁止经命令行传递**（SOP 2.3）——所有中文一律进程内赋值。
"""

from __future__ import annotations

import sys
from pathlib import Path

# ⚠️ 编码必须最先设置：早于任何 import 触发的 IO
from infra.encoding import force_utf8_encoding

force_utf8_encoding()

from infra.config import AppConfig  # noqa: E402
from infra.errors import CustomsCheckerError, user_message_of  # noqa: E402
from infra.logger import Phase, get_logger, setup_logging  # noqa: E402

__all__ = ["main", "build_application", "create_main_window"]

APP_NAME = "报关申报要素自动校验工具"
#: 交付版本号（对外）。与《架构设计》文档的版本号（v1.2）**属不同体系**，勿混用。
#: ⚠️ 改这里时**必须同步** ``ui/main_window.py::APP_VERSION``（同一版本号两处定义）
APP_VERSION = "0.3.1"
ORG_NAME = "CustomsChecker"


def _default_log_path(config: AppConfig) -> Path | None:
    """推导本次会话的日志落点（过程产出 ``logs`` 目录，v0.2.0 运行目录约定）。

    Args:
        config: 应用配置（**仅用其票号做文件名**；不再读取 ``result_dir`` /
            ``process_dir`` —— Q8 已把这些字段降级为兼容占位）。

    Returns:
        日志文件路径；目录无法确定时返回 ``None``（仅控制台/信号日志）。

    Note:
        落点恒为 ``app_base_dir()/报关申报要素校验/logs``（打包态 = exe 同目录，
        开发态 = 工程根），由 :meth:`app.path_policy.PathPolicy.resolve_outputs`
        解析并**幂等创建**。**绝不**用 ``__file__`` 兜底 —— 单文件（onefile）打包态下
        ``__file__`` 位于 ``%TEMP%\\_MEIxxxxxx`` 临时解包目录，进程退出即被删除，
        日志会随之消失（违反「可追溯」红线）。
    """
    from app.path_policy import PROCESS_DIR_NAME, WORKSPACE_DIR_NAME, PathPolicy

    try:
        _result_dir, process_dir = PathPolicy().resolve_outputs()
    except Exception:  # noqa: BLE001 - 日志落点推导不得阻断启动
        from infra.resources import app_base_dir

        process_dir = app_base_dir() / WORKSPACE_DIR_NAME / PROCESS_DIR_NAME
        try:
            process_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    ticket = config.ticket_no.strip() or "未命名"
    return Path(process_dir) / f"校验运行日志_{ticket}.log"


def _load_rules(log) -> None:
    """启动一次性加载规则快照（v1.1 C.3）。

    Args:
        log: 日志器。

    Raises:
        CustomsCheckerError: 规则缺失 / 校验失败（RuleConfigError）。
    """
    from core.rule_repository import RuleRepository

    repo = RuleRepository()
    snapshot = repo.load_all()
    warnings = repo.validate()

    for note in repo.load_notes:
        log.info(note)
    log.info(
        "规则已加载：黑名单 %d 条 / 品牌正则 %d 条 / 易混字符组 %d 组",
        len(snapshot.fields_blacklist.terms),
        len(snapshot.brand_patterns.patterns),
        len(snapshot.noise_signals.confusable_chars),
    )
    for warn in warnings:
        log.warn(warn)


def _configure_dpi() -> None:
    """设置高 DPI 缩放策略（12.B.3 注意事项 #4）。

    必须**在 QApplication 实例化之前**调用。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


def build_application(argv: list[str] | None = None):
    """创建 :class:`QApplication`（已完成 DPI 策略设置）。

    Args:
        argv: 命令行参数（缺省 ``sys.argv``）。

    Returns:
        ``QApplication`` 实例。
    """
    from PySide6.QtWidgets import QApplication

    _configure_dpi()

    app = QApplication.instance()
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)
    return app


def create_main_window(config: AppConfig):
    """创建主窗口（延迟 import，保证引擎可脱离 GUI 单测）。

    Args:
        config: 应用配置。

    Returns:
        ``MainWindow`` 实例。
    """
    from ui.main_window import MainWindow

    return MainWindow(config)


def _apply_style(app) -> None:
    """加载并应用全局 QSS（``ui/styles/app.qss``）。

    样式缺失时静默降级（不阻断启动）。

    Args:
        app: ``QApplication`` 实例。
    """
    try:
        from ui.styles.palette import apply_stylesheet

        apply_stylesheet(app)
    except Exception as exc:  # noqa: BLE001 - 样式失败不得阻断启动
        get_logger(Phase.APP).warn("样式加载失败（已忽略）：%s", exc)


def _run_self_test(log) -> int:
    """``--self-test``：跑自检后退出（Q8 隐藏参数）。

    Returns:
        退出码（0 通过 / 1 失败）。
    """
    try:
        from core.constants import COLUMN_COUNT, VERDICT_NO_MARK, VERDICT_PASS
        from core.models import Verdict, build_key
        from core.rule_repository import RuleRepository

        repo = RuleRepository()
        repo.load_all()
        repo.validate()

        assert COLUMN_COUNT == 13, "汇总表必须恰好 13 列"
        assert VERDICT_PASS == "✅ 校验合格", "判定字符串必须与 SOP 1.3 逐字一致"
        assert Verdict.NO_MARK.value == "NO_MARK", "枚举值异常"
        assert build_key("A", "B", "C") == "A|B|C", "key 构造规则异常"

        log.info("自检通过：规则加载 OK / 13 列 OK / 口径字符串 OK")
        print("SELF-TEST: PASS")
        print(f"  VERDICT_PASS   = {VERDICT_PASS}")
        print(f"  VERDICT_NO_MARK= {VERDICT_NO_MARK}")
        print(f"  COLUMNS        = {COLUMN_COUNT}")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 自检需打印一切失败原因
        log.error("自检失败：%s", exc)
        print(f"SELF-TEST: FAIL - {type(exc).__name__}: {exc}")
        return 1


def _run_env_check() -> int:
    """``--check-env``：调用环境体检脚本后退出。

    Note:
        必须同时兼容三种布局，否则**打包后会静默失效**（且窗口模式下
        未捕获异常会弹出模态错误框把进程挂死，现场看起来就是"命令没反应"）：

        * onefile 打包态  → ``tools/check_env.py`` 随包解到 ``bundle_root()``（``_MEIPASS``）
        * 目录形式打包态  → ``tools/`` 与 exe 同目录，即 ``app_base_dir() / "tools"``
        * 开发态          → ``__file__`` 所在目录下的 ``tools/``

    Returns:
        ``tools/check_env.py`` 的退出码；脚本不可用时返回 ``1``。
    """
    from infra.resources import app_base_dir, bundle_root

    candidates = [
        bundle_root() / "tools",  # onefile：随包解出
        app_base_dir() / "tools",  # 目录形式 / 现场工程师手放的 tools/
        Path(__file__).resolve().parent / "tools",  # 开发态
    ]
    tools_dir = next((p for p in candidates if (p / "check_env.py").is_file()), None)
    if tools_dir is None:
        print("环境体检不可用：未找到 tools/check_env.py")
        print("  已尝试：")
        for candidate in candidates:
            print(f"    - {candidate}")
        return 1

    sys.path.insert(0, str(tools_dir))
    try:
        import check_env  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - CLI 必须给出可读失败原因而非堆栈
        print(f"环境体检不可用：导入 check_env 失败 - {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            sys.path.remove(str(tools_dir))
        except ValueError:
            pass

    report = check_env.run_check()
    print(report.render())
    return 1 if report.fatal else (2 if report.version_warnings else 0)


def main(argv: list[str] | None = None) -> int:
    """程序主入口。

    Args:
        argv: 命令行参数（缺省 ``sys.argv[1:]``）。

    Returns:
        进程退出码。
    """
    args = list(sys.argv[1:] if argv is None else argv)

    # ── 隐藏 CLI 参数（Q8）：不进入 GUI ──
    if "--check-env" in args:
        return _run_env_check()

    config = AppConfig.load()

    log_path = _default_log_path(config)
    setup_logging(log_path, console=True)
    log = get_logger(Phase.APP)

    log.info("=" * 56)
    log.info("%s v%s 启动", APP_NAME, APP_VERSION)
    log.info("Python %s", sys.version.split()[0])
    if log_path is not None:
        log.info("日志落点：%s", log_path)

    if "--self-test" in args:
        return _run_self_test(log)

    try:
        _load_rules(log)
    except CustomsCheckerError as exc:
        log.error("规则加载失败：%s", exc.user_message)

    try:
        app = build_application([sys.argv[0]])
        _apply_style(app)
        window = create_main_window(config)
        window.show()
        log.info("主窗口已显示，进入事件循环")
        return int(app.exec())
    except CustomsCheckerError as exc:
        log.error("启动失败：%s", exc.user_message)
        print(user_message_of(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免"闪退无提示"
        log.error("未预期异常：%s: %s", type(exc).__name__, exc)
        print(f"启动失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
