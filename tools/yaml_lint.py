"""校验仓库内所有 YAML：拒绝重复映射键，支持多文档与 << merge key。

CI 的 yaml lint job 直接调用本文件，因此本地与 CI 的行为完全一致。
merge key（`<<: *anchor` 或 `<<: {a: 1}`）是 YAML 允许的复用写法，其展开出的
同名键被显式键覆盖不算重复；但合并源自身内部的重复键必须拒绝。
"""
from __future__ import annotations

import pathlib
import sys

import yaml

MERGE_TAG = "tag:yaml.org,2002:merge"
SKIP = {".git", "backups", "data", "data_live"}


def _check_node(node: yaml.Node | None, visited: set[int]) -> None:
    """遍历节点树：同一映射内出现重复键即报错，merge 展开出的映射同样检查。"""
    if node is None or id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.tag != MERGE_TAG:
                if key_node.value in keys:
                    line = key_node.start_mark.line + 1
                    raise ValueError(f"duplicate mapping key: {key_node.value!r} (line {line})")
                keys.add(key_node.value)
            _check_node(value_node, visited)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _check_node(item, visited)


def main() -> int:
    candidates = sorted(pathlib.Path(".").rglob("*.yml")) + sorted(
        pathlib.Path(".").rglob("*.yaml")
    )
    paths = [p for p in candidates if not any(part in SKIP for part in p.parts)]
    bad: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            bad.append(f"{path}: 不是 UTF-8 编码: {exc}")
            continue
        try:
            for document in yaml.compose_all(text, Loader=yaml.SafeLoader):
                _check_node(document, set())
        except yaml.YAMLError as exc:
            bad.append(f"{path}: {exc}")
        except ValueError as exc:
            bad.append(f"{path}: {exc}")
    print("\n".join(bad) if bad else f"all {len(paths)} YAML files parsed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
