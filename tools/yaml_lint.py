"""校验仓库内所有 YAML：拒绝重复映射键，支持多文档与 << merge key。

CI 的 yaml lint job 直接调用本文件，因此本地与 CI 的行为完全一致。

检查分两步，缺一不可：

1. 节点树遍历（Loader.get_node 逐文档取节点）：`<<` 合并进来的映射由 `flatten_mapping`
   直接展开，不经过构造阶段的键检查，所以 `<<: {a: 1, a: 2}` 这类重复只能在这一层发现。
2. 完整构造（`yaml.safe_load_all`）：只拿到节点树不做构造校验，`<<: 1` 这类非法合并值、
   无法构造的 tag 只有构造阶段才会报错。

重复键的身份按构造后的值判定，而不是原始字符串：`1` 与 `0x1` 是同一个整数键
（后者覆盖前者），`on` 与 `true` 都是布尔 True，`1` 与 `'1'` 则不是同一个键。

扫描范围是仓库根（脚本所在目录的上一级），与运行时的 CWD 无关。
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

import yaml

MERGE_TAG = "tag:yaml.org,2002:merge"
SKIP = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".tox",
    "backups",
    "data",
    "data_live",
}
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _check_node(node: yaml.Node | None, loader: yaml.SafeLoader, visited: set[int]) -> None:
    """遍历节点树：同一映射内出现重复键即报错，merge 展开出的映射同样检查。"""
    if node is None or id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, yaml.MappingNode):
        # 用 dict 的键语义判重，与构造阶段的实际覆盖行为一致：
        # 1 与 0x1 都构造为 int 1，算重复；1 与 '1' 类型不同，不算重复。
        seen: dict[Any, None] = {}
        for key_node, value_node in node.value:
            if key_node.tag != MERGE_TAG:
                key: Any = None
                try:
                    key = loader.construct_object(key_node, deep=True)
                    duplicated = key in seen
                except (yaml.YAMLError, TypeError):
                    # 构造不出来或不可 hash 的键：这里判不了，交给第二步的完整构造报错。
                    duplicated = False
                if duplicated:
                    line = key_node.start_mark.line + 1
                    raise ValueError(f"duplicate mapping key: {key!r} (line {line})")
                if key is not None:
                    try:
                        seen[key] = None
                    except TypeError:
                        pass
            _check_node(value_node, loader, visited)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _check_node(item, loader, visited)


def validate(text: str) -> None:
    """对单个文件的文本做两步校验，失败时抛 ValueError。"""
    loader = yaml.SafeLoader(text)
    try:
        while loader.check_node():
            _check_node(loader.get_node(), loader, set())
    except yaml.YAMLError as exc:
        raise ValueError(" ".join(str(exc).split())) from exc
    finally:
        loader.dispose()
    # 第二步：完整构造一遍，捕获节点遍历看不到的错误（非法 merge 值、未知 tag 等）。
    try:
        for _ in yaml.safe_load_all(text):
            pass
    except yaml.YAMLError as exc:
        raise ValueError(" ".join(str(exc).split())) from exc


def main() -> int:
    candidates = sorted(ROOT.rglob("*.yml")) + sorted(ROOT.rglob("*.yaml"))
    paths = [p for p in candidates if not any(part in SKIP for part in p.relative_to(ROOT).parts)]
    bad: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            bad.append(f"{path.relative_to(ROOT)}: 不是 UTF-8 编码: {exc}")
            continue
        try:
            validate(text)
        except ValueError as exc:
            bad.append(f"{path.relative_to(ROOT)}: {exc}")
    print("\n".join(bad) if bad else f"all {len(paths)} YAML files parsed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
