"""tools/yaml_lint.py 的回归：重复键必须拒绝，合法 merge 与多文档必须通过。"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

TOOL = pathlib.Path(__file__).resolve().parents[1] / "tools" / "yaml_lint.py"


def _load_tool():
    if not TOOL.exists():
        # 容器 smoke 只挂载 tests/ 时没有 tools/，跳过而不是在 collection 阶段抛 FileNotFoundError。
        pytest.skip(f"缺少 {TOOL}，跳过 yaml_lint 用例", allow_module_level=True)
    spec = importlib.util.spec_from_file_location("yaml_lint_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL_MODULE = _load_tool()


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
        # 整数键与字符串键类型不同，构造后不互相覆盖，不算重复
        "1: a\n'1': b\n",
        # 自引用别名不能死循环
        "a: &a [*a]\n",
        # 空文档
        "",
    ],
)
def test_accepts_valid_yaml(text: str) -> None:
    TOOL_MODULE.validate(text)


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
        # 0x1 与 1 构造后是同一个整数键，后者会静默覆盖前者
        "1: a\n0x1: b\n",
        # true 与 yes 构造后都是布尔 True
        "true: a\nyes: b\n",
        # 复合键（列表）重复：旧实现按「仅标量」判重会完全跳过
        "? [a, b]\n: 1\n? [a, b]\n: 2\n",
        # 未知 tag 的键：判重时构造失败，必须由完整构造步骤报错而不是抛 NameError
        "!!foo bar: 1\n",
        # 非法 merge 值：节点树看不出问题，只有完整构造阶段才报错
        "z:\n  <<: 1\n",
        "z:\n  <<: [1, 2]\n",
    ],
)
def test_rejects_duplicate_or_invalid_yaml(text: str) -> None:
    with pytest.raises(ValueError):
        TOOL_MODULE.validate(text)


def _run_main(monkeypatch, tmp_path: pathlib.Path, capsys):
    monkeypatch.setattr(TOOL_MODULE, "ROOT", tmp_path)
    code = TOOL_MODULE.main()
    return code, capsys.readouterr().out


def test_main_reports_filtered_file_count(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "ok.yml").write_text("a: 1\n", encoding="utf-8")
    # backups/ 在 SKIP 里，里面的坏文件不应参与统计
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "bad.yml").write_text("a: 1\na: 2\n", encoding="utf-8")

    code, out = _run_main(monkeypatch, tmp_path, capsys)

    assert code == 0
    assert out.strip() == "all 1 YAML files parsed"


def test_main_fails_on_duplicate_key(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "dup.yml").write_text("a: 1\na: 2\n", encoding="utf-8")

    code, out = _run_main(monkeypatch, tmp_path, capsys)

    assert code == 1
    assert "duplicate mapping key" in out
    assert "dup.yml" in out


def test_main_reports_non_utf8_file(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "gbk.yml").write_bytes("名称: 值\n".encode("gbk"))

    code, out = _run_main(monkeypatch, tmp_path, capsys)

    assert code == 1
    assert "不是 UTF-8 编码" in out


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
        TOOL_MODULE.validate(path.read_text(encoding="utf-8"))
