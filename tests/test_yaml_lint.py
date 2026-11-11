"""tools/yaml_lint.py 的回归：重复键必须拒绝，合法 merge 与多文档必须通过。"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

TOOL = pathlib.Path(__file__).resolve().parents[1] / "tools" / "yaml_lint.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("yaml_lint_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL_MODULE = _load_tool()


def _validate(text: str) -> None:
    for document in yaml.compose_all(text, Loader=yaml.SafeLoader):
        TOOL_MODULE._check_node(document, set())


@pytest.mark.parametrize(
    "text",
    [
        "ok: 1\n",
        # 多文档：yaml.load 会报错，校验必须支持
        "a: 1\n---\nb: 2\n",
        # 合法 merge key
        "x: &x {a: 1, b: 2}\nz:\n  <<: *x\n",
        # 显式键覆盖 merge 进来的同名键是 YAML 允许的写法
        "x: &x {a: 1, b: 2}\nz:\n  <<: *x\n  a: 9\n",
        # 内联 merge 映射
        "z:\n  <<: {a: 1, b: 2}\n  a: 9\n",
        # 多 anchor 合并
        "x: &x {a: 1}\ny: &y {b: 2}\nz:\n  <<: [*x, *y]\n",
    ],
)
def test_accepts_valid_yaml(text: str) -> None:
    _validate(text)


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",
        # merge 之后仍有真实重复
        "x: &x {a: 1}\nz:\n  <<: *x\n  a: 9\n  a: 8\n",
        # 内联 merge 映射内部的重复键：flatten_mapping 会直接展开，必须由节点遍历发现
        "z:\n  <<: {a: 1, a: 2}\n",
        # 嵌套映射里的重复
        "outer:\n  inner:\n    a: 1\n    a: 2\n",
    ],
)
def test_rejects_duplicate_keys(text: str) -> None:
    with pytest.raises(ValueError, match="duplicate mapping key"):
        _validate(text)


def test_repository_yaml_files_pass() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    candidates = sorted(root.rglob("*.yml")) + sorted(root.rglob("*.yaml"))
    paths = [
        p
        for p in candidates
        if not any(part in TOOL_MODULE.SKIP for part in p.relative_to(root).parts)
    ]
    if not paths:
        # 容器里只挂载了 tests/ 与 tools/，没有仓库级 YAML 可校验；CI 的 yaml lint job 会覆盖。
        pytest.skip("当前目录下没有 YAML 文件")
    for path in paths:
        _validate(path.read_text(encoding="utf-8"))
