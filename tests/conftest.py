"""pytest 公共夹具（tests/conftest.py）。

提供：
  * ``project_root``        —— 工程根路径
  * ``rules_dir``           —— 内置规则目录
  * ``sample_dir``          —— 真实样本目录（存在则提供，否则 skip）
  * ``temp_project``        —— 临时工程空间（成果产出 / 过程产出 / 原始输入）
  * ``rule_repo``           —— 已加载的内置规则仓库
  * ``fake_ocr_backend``    —— 离线 OCR mock 后端（不依赖真实 rapidocr）
  * ``sample_element_texts``—— 申报要素解析回归样本
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ── 路径常量 ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = PROJECT_ROOT / "rules"

# 真实样本目录（架构设计/PRD 提到的 SA26090215 样本）；不存在则相关用例 skip
SAMPLE_CANDIDATES = [
    PROJECT_ROOT / "原始输入",
    Path(r"D:\Data\workspace\workdoc\AI测试\原始输入"),
    Path(r"D:\Data\workspace\workdoc\AI测试"),
]


def _first_existing(candidates: list[Path]) -> Path | None:
    """返回第一个存在的候选路径。"""
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@pytest.fixture(scope="session")
def project_root() -> Path:
    """工程根目录。"""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def rules_dir() -> Path:
    """内置规则目录 ``rules/``。"""
    return RULES_DIR


@pytest.fixture(scope="session")
def sample_dir() -> Path:
    """真实样本目录；不存在时跳过依赖样本的用例。"""
    found = _first_existing(SAMPLE_CANDIDATES)
    if found is None:
        pytest.skip("真实样本目录不存在，跳过需要样本的用例")
    return found


@pytest.fixture
def temp_project(tmp_path: Path) -> dict[str, Path]:
    """临时工程空间：三铁律所需的目录结构。

    Returns:
        ``{"root", "input", "result", "process"}`` 四个路径。
    """
    root = tmp_path / "项目空间"
    structure = {
        "root": root,
        "input": root / "原始输入",
        "result": root / "成果产出",
        "process": root / "过程产出",
    }
    for key, path in structure.items():
        if key == "root":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return structure


@pytest.fixture
def rule_repo(rules_dir: Path):
    """已加载内置规则的 :class:`RuleRepository`（用户目录指向空临时目录）。"""
    from core.rule_repository import RuleRepository

    repo = RuleRepository(builtin_dir=rules_dir)
    repo.load_all()
    return repo


@pytest.fixture
def fake_ocr_backend():
    """离线 OCR mock 后端（不依赖真实 rapidocr / onnxruntime）。

    返回一个可调用对象：``fake(text_map: dict[str, str]) -> MockOcrBackend``，
    按图片路径返回预设文本。

    Returns:
        工厂函数。
    """

    class MockOcrText:
        """模拟 OCR 结果对象。"""

        def __init__(self, image_path: str, text: str, confidence: float = 0.95) -> None:
            self.image_path = image_path
            self.text_raw = text
            self.confidence = confidence
            self.boxes: list[tuple[int, int, int, int]] = [(0, 0, 10, 10)]
            self.seq = 0

    class MockOcrBackend:
        """离线 mock 后端：按路径查表返回文本。"""

        def __init__(self, text_map: dict[str, str] | None = None) -> None:
            self.text_map = text_map or {}
            self.calls: list[str] = []
            self._warmed = False

        def recognize(self, image_path) -> MockOcrText:
            path_str = str(image_path)
            self.calls.append(path_str)
            text = self.text_map.get(path_str) or self.text_map.get(Path(path_str).name, "")
            return MockOcrText(path_str, text)

        def warmup(self) -> None:
            self._warmed = True

    def factory(text_map: dict[str, str] | None = None) -> MockOcrBackend:
        return MockOcrBackend(text_map)

    return factory


@pytest.fixture
def sample_element_texts() -> dict[str, dict[str, str]]:
    """申报要素解析回归样本（覆盖 SOP 实测 5 种品牌形态 + 脏尾 + 零宽字符）。

    Returns:
        ``{用例名: {"raw": 原文, "brand": 期望品牌, "model": 期望型号}}``。
    """
    return {
        "ascii_colon": {
            "raw": "品牌:baori|型号:A7A01G ，电视机用/",
            "brand": "baori",
            "model": "A7A01G",
        },
        "none_colon": {
            "raw": "品牌:无|型号:无",
            "brand": "",
            "model": "",
        },
        "none_nocolon": {
            "raw": "无品牌|型号:HS-8A50J-12  蓝牙遥控器",
            "brand": "",
            "model": "HS-8A50J-12",
        },
        "chinese_suffix": {
            "raw": "宇同品牌、型号：YT-100",
            "brand": "宇同",
            "model": "YT-100",
        },
        "semicolon_empty": {
            "raw": "品牌;无;|型号;GXD-009;",
            "brand": "",
            "model": "GXD-009",
        },
        "zero_width": {
            "raw": "品牌:COOCAA\u200b|型号:2.402GHz",
            "brand": "COOCAA",
            "model": "2.402GHz",
        },
        "single_pai": {
            "raw": "SAMSUNG牌|型号:UA55",
            "brand": "SAMSUNG",
            "model": "UA55",
        },
    }
