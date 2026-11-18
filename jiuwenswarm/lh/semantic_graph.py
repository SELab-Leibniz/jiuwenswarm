# Copyright (c) 2026 Chrys. All rights reserved.

"""Read-only repository units, qualified Python call edges and graph queries."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import threading
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

GRAPH_VERSION = "frozen-s0-qualified-python-graph/v1"
IGNORE_DIRS = {
    ".git",
    ".hg",
    ".venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".codegraph",
    ".semantic-search",
    ".pact",
    ".pact-io",
    "__pycache__",
    "node_modules",
    "vendor",
    "build",
    "dist",
    "target",
    "dockers",
    "patches",
    "requirement_pr_pairs",
    "ground_truth",
}
TEXT_SUFFIXES = {
    ".py",
    ".sh",
    ".go",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".md",
    ".rst",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
}
CALLBACKS = {
    "create_task": 0,
    "ensure_future": 0,
    "submit": 0,
    "map": 0,
    "add_done_callback": 0,
    "call_soon": 0,
    "call_later": 1,
    "run_in_executor": 1,
}


def expression(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = expression(node.value)
        return prefix + "." + node.attr if prefix else ""
    if isinstance(node, ast.Subscript):
        return expression(node.value)
    if isinstance(node, ast.Call) and expression(node.func) == "super":
        return "super"
    return ""


def body_nodes(node: ast.AST):
    """Walk one scope without attributing nested definitions' calls to it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        yield from body_nodes(child)


@dataclass(frozen=True)
class Unit:
    id: str
    file_path: str
    symbol: str
    kind: str
    start_line: int
    end_line: int
    parent: str = ""
    owner_class: str = ""


class RepositoryGraph:
    """Conservative static Python graph; unresolved dispatch never gets guessed."""

    def __init__(self, root: Path, *, stop: threading.Event | None = None, max_files: int = 20_000):
        self.root = root.resolve()
        self.stop = stop or threading.Event()
        self.files: dict[str, str] = {}
        self.units: dict[str, Unit] = {}
        self.by_file: dict[str, list[Unit]] = defaultdict(list)
        self.by_symbol: dict[tuple[str, str], Unit] = {}
        self.modules: dict[str, set[str]] = defaultdict(set)
        self.file_module: dict[str, str] = {}
        self.trees: dict[str, ast.Module] = {}
        self.nodes: dict[str, ast.AST] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        self.scopes: dict[str, tuple[dict[str, str], set[str]]] = {}
        self.edges: set[tuple[str, str, str]] = set()
        self.adjacent: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.issues: list[dict[str, str]] = []
        self.truncated = False
        self.unresolved_calls = 0
        for base, dirs, names in os.walk(self.root, followlinks=False):
            self._check_stop()
            dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not (Path(base) / d).is_symlink())
            for name in sorted(names):
                self._check_stop()
                path = Path(base) / name
                if path.is_symlink() or path.suffix not in TEXT_SUFFIXES:
                    continue
                relative = path.relative_to(self.root).as_posix()
                if name in {"reward.json", "test_patch.txt"} or name.startswith(".chrys_snapshot"):
                    continue
                if len(self.files) >= max_files:
                    self.truncated = True
                    break
                try:
                    if path.stat().st_size > 350_000:
                        self.issues.append({"file": relative, "reason": "file_size_limit"})
                        continue
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    self.issues.append({"file": relative, "reason": "unreadable_text"})
                    continue
                self.files[relative] = text
                if path.suffix == ".py":
                    self._index_python(relative, text)
            if self.truncated:
                break
        for relative, tree in self.trees.items():
            self.aliases[relative] = self._imports(relative, list(body_nodes(tree)))
        for unit in self.units.values():
            self._check_stop()
            self.scopes[unit.id] = self._scope(unit)
        self._build_edges()

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise InterruptedError("repository graph build cancelled")

    def _index_python(self, relative: str, text: str) -> None:
        try:
            tree = ast.parse(text, filename=relative)
        except (SyntaxError, RecursionError):
            self.issues.append({"file": relative, "reason": "python_parse_failed"})
            return
        self.trees[relative] = tree
        parts = list(Path(relative).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        variants = [parts]
        if parts and parts[0] in {"src", "lib", "python", "python_src"}:
            variants.append(parts[1:])
        self.file_module[relative] = ".".join(variants[-1])
        for variant in variants:
            if variant:
                self.modules[".".join(variant)].add(relative)

        def definitions(body: list[ast.stmt], prefix: str = "", owner: str = "", parent: str = "") -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbol = prefix + "." + node.name if prefix else node.name
                    key = relative + ":" + symbol
                    kind = "class" if isinstance(node, ast.ClassDef) else "method" if owner else "function"
                    unit = Unit(key, relative, symbol, kind, node.lineno, node.end_lineno or node.lineno, parent, owner)
                    self.units[key] = unit
                    self.by_symbol[(relative, symbol)] = unit
                    self.by_file[relative].append(unit)
                    self.nodes[key] = node
                    definitions(node.body, symbol, symbol if isinstance(node, ast.ClassDef) else owner, key)
                elif isinstance(node, ast.If):
                    definitions(node.body, prefix, owner, parent)
                    definitions(node.orelse, prefix, owner, parent)
                elif isinstance(node, ast.Try):
                    for branch in [node.body, node.orelse, node.finalbody, *(h.body for h in node.handlers)]:
                        definitions(branch, prefix, owner, parent)

        definitions(tree.body)

    def _imports(self, relative: str, nodes: list[ast.AST]) -> dict[str, str]:
        result: dict[str, str] = {}
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    package = self.file_module[relative].split(".")
                    if not relative.endswith("/__init__.py"):
                        package = package[:-1]
                    package = package[: max(0, len(package) - node.level + 1)]
                    module = ".".join([*package, *([module] if module else [])])
                for alias in node.names:
                    if alias.name != "*":
                        result[alias.asname or alias.name] = ".".join(filter(None, [module, alias.name]))
        return result

    def _scope(self, unit: Unit) -> tuple[dict[str, str], set[str]]:
        aliases = dict(self.aliases[unit.file_path])
        lineage: list[Unit] = []
        parent = self.units.get(unit.parent)
        while parent:
            if parent.kind != "class":
                lineage.append(parent)
            parent = self.units.get(parent.parent)
        shadowed: set[str] = set()
        for ancestor in reversed(lineage):
            ancestor_aliases, ancestor_shadowed = self.scopes[ancestor.id]
            aliases.update(ancestor_aliases)
            shadowed.update(ancestor_shadowed)
            for name in ancestor_shadowed:
                aliases.pop(name, None)
        nodes = list(body_nodes(self.nodes[unit.id]))
        aliases.update(self._imports(unit.file_path, nodes))
        node = self.nodes[unit.id]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                *([node.args.vararg] if node.args.vararg else []),
                *([node.args.kwarg] if node.args.kwarg else []),
            ]:
                target = expression(arg.annotation)
                if target:
                    aliases[arg.arg] = aliases.get(target, target)
                else:
                    shadowed.add(arg.arg)
                    aliases.pop(arg.arg, None)
        for child in nodes:
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                value = child.value
                target = expression(value.func) if isinstance(value, ast.Call) else expression(value)
                if isinstance(child, ast.AnnAssign) and not target:
                    target = expression(child.annotation)
                for assigned in targets:
                    if isinstance(assigned, ast.Name):
                        if target:
                            first, dot, rest = target.partition(".")
                            aliases[assigned.id] = aliases.get(first, first) + (dot + rest if dot else "")
                        else:
                            shadowed.add(assigned.id)
                            aliases.pop(assigned.id, None)
        return aliases, shadowed

    def _qualified(self, value: str, *, depth: int = 0) -> list[Unit]:
        if depth > 8:
            return []
        parts = value.split(".")
        for split in range(len(parts) - 1, 0, -1):
            module, symbol = ".".join(parts[:split]), ".".join(parts[split:])
            files = self.modules.get(module, set())
            if len(files) != 1:
                continue
            relative = next(iter(files))
            direct = self.by_symbol.get((relative, symbol))
            if direct:
                return [direct]
            first, dot, rest = symbol.partition(".")
            alias = self.aliases.get(relative, {}).get(first)
            if alias:
                return self._qualified(alias + (dot + rest if dot else ""), depth=depth + 1)
        return []

    def resolve(self, value: str, source: Unit) -> list[Unit]:
        if not value:
            return []
        aliases, shadowed = self.scopes[source.id]
        first, dot, rest = value.partition(".")
        if first in {"self", "cls"} and source.owner_class and dot:
            direct = self.by_symbol.get((source.file_path, source.owner_class + "." + rest))
            return [direct] if direct else self._base_method(source, rest)
        if first == "super" and dot:
            return self._base_method(source, rest)
        if first in aliases:
            target = aliases[first] + (dot + rest if dot else "")
            imported = self._qualified(target)
            if imported:
                return imported
            direct = self.by_symbol.get((source.file_path, target))
            return [direct] if direct else []
        if first in shadowed:
            return []
        parent = self.units.get(source.parent)
        while parent:
            if parent.kind != "class":
                direct = self.by_symbol.get((source.file_path, parent.symbol + "." + value))
                if direct:
                    return [direct]
            parent = self.units.get(parent.parent)
        direct = self.by_symbol.get((source.file_path, value))
        return [direct] if direct else []

    def _base_method(self, source: Unit, name: str) -> list[Unit]:
        owner = self.by_symbol.get((source.file_path, source.owner_class))
        if not owner:
            return []
        node = self.nodes[owner.id]
        # Conditional definitions can reuse a class's qualified name for a
        # function. The static index cannot resolve that runtime binding; keep
        # its base-method dispatch unresolved instead of aborting the graph.
        if not isinstance(node, ast.ClassDef):
            return []
        results = []
        for base in node.bases:
            for target in self.resolve(expression(base), owner):
                method = self.by_symbol.get((target.file_path, target.symbol + "." + name))
                if method:
                    results.append(method)
        return results

    def _edge(self, source: Unit, target: Unit, kind: str, *, reverse: str | None = None) -> None:
        edge = (source.id, target.id, kind)
        if edge not in self.edges:
            self.edges.add(edge)
            self.adjacent[source.id].append((kind, target.id))
        if reverse:
            self._edge(target, source, reverse)

    def _build_edges(self) -> None:
        for unit in self.units.values():
            self._check_stop()
            if unit.parent:
                self._edge(self.units[unit.parent], unit, "child", reverse="parent")
            node = self.nodes[unit.id]
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    for target in self.resolve(expression(base), unit):
                        self._edge(unit, target, "inherits", reverse="inherited_by")
                continue
            for child in body_nodes(node):
                if isinstance(child, ast.Call):
                    name = expression(child.func)
                    targets = self.resolve(name, unit)
                    if not targets:
                        self.unresolved_calls += 1
                    for target in targets:
                        kind = "call" if target.file_path == unit.file_path else "cross_file_call"
                        self._edge(unit, target, kind, reverse="caller")
                    callback = CALLBACKS.get(name.rsplit(".", 1)[-1])
                    if callback is not None and len(child.args) > callback:
                        arg = child.args[callback]
                        for target in self.resolve(expression(arg.func if isinstance(arg, ast.Call) else arg), unit):
                            self._edge(unit, target, "async_call", reverse="caller")
            if unit.owner_class:
                for base_method in self._base_method(unit, unit.symbol.rsplit(".", 1)[-1]):
                    self._edge(unit, base_method, "override", reverse="overridden_by")

    def summary(self) -> dict[str, Any]:
        return {
            "backend": GRAPH_VERSION,
            "file_count": len(self.files),
            "unit_count": len(self.units),
            "edge_count": len(self.edges),
            "unresolved_calls": self.unresolved_calls,
            "truncated": self.truncated,
            "issues": self.issues,
            "external_codegraph_used": False,
            "limitations": [
                "Python static imports and known receivers only; dynamic dispatch is unresolved.",
                "Other languages have file/content search, without claimed semantic edges.",
            ],
        }

    def export(self) -> dict[str, Any]:
        return {
            "schema": "long-horizon/repository-graph/v1",
            "summary": self.summary(),
            "files": [
                {"file_path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                for path, text in sorted(self.files.items())
            ],
            "nodes": [asdict(unit) for unit in self.units.values()],
            "edges": [
                {"source": source, "target": target, "kind": kind} for source, target, kind in sorted(self.edges)
            ],
        }

    def describe(self, unit: Unit) -> dict[str, Any]:
        lines = self.files[unit.file_path].splitlines()
        return {
            **asdict(unit),
            "preview": "\n".join(
                f"{i + 1}: {lines[i]}" for i in range(unit.start_line - 1, min(unit.end_line, unit.start_line + 18))
            )[:2200],
            "relationships": [
                {
                    "kind": kind,
                    "target": target,
                    "file_path": self.units[target].file_path,
                    "symbol": self.units[target].symbol,
                }
                for kind, target in sorted(self.adjacent[unit.id])[:24]
            ],
        }

    def query(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The native five-tool interface, represented as portable JSON results."""
        file_filter = arguments.get("file_path", "*")
        if not isinstance(file_filter, str):
            raise ValueError("file_path must be a string")
        files = [path for path in sorted(self.files) if fnmatch.fnmatchcase(path, file_filter)]
        if name == "finish_search":
            return {"finished": True, "units": [], "files": []}
        if name == "find_file":
            pattern = arguments.get("file_name", "*")
            directory = arguments.get("dir_path", ".")
            if not isinstance(pattern, str) or not isinstance(directory, str):
                raise ValueError("file_name and dir_path must be strings")
            matched = [
                p
                for p in sorted(self.files)
                if (fnmatch.fnmatchcase(p, pattern) or fnmatch.fnmatchcase(Path(p).name, pattern))
                and (directory in (".", "") or p.startswith(directory.rstrip("/") + "/"))
            ]
            return {
                "files": matched[:12],
                "units": [self.describe(u) for p in matched[:12] for u in self.by_file[p][:3]][:8],
                "total_matches": len(matched),
            }
        if name in {"find_code_definition", "find_child_unit"}:
            definition = arguments.get("definition_name")
            if not isinstance(definition, str) or not definition.strip():
                raise ValueError("definition_name is required")
            units = [
                u
                for p in files
                for u in self.by_file[p]
                if fnmatch.fnmatchcase(u.symbol, definition)
                or fnmatch.fnmatchcase(u.symbol.rsplit(".", 1)[-1], definition)
            ]
            return {"files": [], "units": [self.describe(u) for u in units[:8]], "total_matches": len(units)}
        if name == "find_code_content":
            content = arguments.get("content")
            if not isinstance(content, str) or not content or len(content) > 500:
                raise ValueError("content must be a nonempty bounded literal")
            matches, selected = [], {}
            for path in files:
                for line, text in enumerate(self.files[path].splitlines(), 1):
                    if content.casefold() in text.casefold():
                        matches.append({"file_path": path, "line": line, "text": text[:300]})
                        containers = [u for u in self.by_file[path] if u.start_line <= line <= u.end_line]
                        if containers:
                            unit = min(containers, key=lambda u: u.end_line - u.start_line)
                            selected[unit.id] = unit
                        if len(matches) >= 20:
                            break
                if len(matches) >= 20:
                    break
            return {
                "files": sorted({m["file_path"] for m in matches}),
                "matches": matches,
                "units": [self.describe(u) for u in list(selected.values())[:8]],
            }
        raise ValueError("unknown graph query tool")

    def fallback(self, task: str) -> list[dict[str, Any]]:
        terms = {term.lower() for term in re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", task)}
        scored = sorted(
            ((sum(t in (u.symbol + " " + u.file_path).lower() for t in terms), u.id) for u in self.units.values()),
            key=lambda item: (-item[0], item[1]),
        )
        queue = deque((key, 0, "task-symbol match") for score, key in scored[:4] if score)
        selected, visited = [], set()
        while queue and len(selected) < 12:
            key, depth, reason = queue.popleft()
            if key in visited:
                continue
            visited.add(key)
            unit = self.units[key]
            selected.append({"file_path": unit.file_path, "symbol": unit.symbol, "reason": reason, "confidence": "low"})
            if depth < 2:
                queue.extend(
                    (target, depth + 1, f"{kind} edge from {key}")
                    for kind, target in sorted(self.adjacent[key])
                    if kind in {"call", "cross_file_call", "caller", "async_call", "override", "overridden_by"}
                )
        return selected

    def normalize(self, rows: Any, *, observed: set[str] | None = None) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or len(rows) > 12:
            raise ValueError("locations must contain at most twelve objects")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("location must be an object")
            path, symbol = row.get("file_path"), row.get("symbol", row.get("function_name", ""))
            if not isinstance(path, str) or path not in self.files or not isinstance(symbol, str):
                raise ValueError("location does not name an indexed file")
            matches = (
                [u for u in self.by_file[path] if symbol in {u.symbol, u.symbol.rsplit(".", 1)[-1]}] if symbol else []
            )
            if symbol and len(matches) != 1:
                raise ValueError("location symbol is missing or ambiguous")
            unit = matches[0] if matches else None
            key = unit.id if unit else path
            if observed is not None and key not in observed:
                raise ValueError(f"location was not exposed by a graph query: {key}")
            if key in seen:
                continue
            seen.add(key)
            role = row.get(
                "role",
                "validation"
                if {"test", "tests", "testing"}.intersection(Path(path).parts) or Path(path).name.startswith("test_")
                else "primary",
            )
            if role not in {"primary", "propagation", "validation"}:
                raise ValueError("invalid location role")
            confidence = row.get("confidence", "medium")
            if confidence not in {"high", "medium", "low"}:
                raise ValueError("invalid location confidence")
            result.append(
                {
                    "file_path": path,
                    "symbol": unit.symbol if unit else "",
                    "start_line": unit.start_line if unit else 1,
                    "end_line": unit.end_line if unit else max(1, min(len(self.files[path].splitlines()), 20)),
                    "role": role,
                    "reason": str(row.get("reason", "Graph query evidence"))[:300],
                    "confidence": confidence,
                    "must_verify": True,
                }
            )
        return result
