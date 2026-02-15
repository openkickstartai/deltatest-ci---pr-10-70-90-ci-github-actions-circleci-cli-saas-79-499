#!/usr/bin/env python3
"""DeltaTest - CI Test Impact Analysis Engine. Only run affected tests."""
import ast
import os
import subprocess
import json
import sys
from pathlib import Path
from collections import defaultdict, deque

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}


def get_changed_files(base="main", root="."):
    """Get .py files changed between base branch and HEAD."""
    for cmd in [
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, "HEAD"],
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=root, check=True)
            return [f for f in r.stdout.strip().split("\n") if f.strip().endswith(".py")]
        except subprocess.CalledProcessError:
            continue
    return []


def extract_imports(filepath):
    """Parse Python AST to extract all imported module names (absolute only)."""
    try:
        source = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, ValueError, OSError):
        return []
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level and node.level > 0:
                continue  # skip relative in basic version
            imports.append(node.module)
    return imports


def _is_test(path):
    """Check if a file path looks like a test file."""
    b = os.path.basename(path)
    return b.startswith("test_") or b.endswith("_test.py")


def _py_files(root):
    """Collect all .py files, skipping venvs and caches."""
    rp = Path(root).resolve()
    return [p for p in rp.rglob("*.py") if not SKIP_DIRS.intersection(p.relative_to(rp).parts)]


def _filepath_to_module(filepath, root):
    """Convert a file path to a dotted module name relative to root."""
    rel = os.path.relpath(str(filepath), str(root))
    rel = rel.replace(os.sep, "/")
    if rel.endswith("/__init__.py"):
        mod = rel[: -len("/__init__.py")].replace("/", ".")
    elif rel.endswith(".py"):
        mod = rel[:-3].replace("/", ".")
    else:
        return None
    return mod if mod else None


def _is_package_init(filepath):
    """Check whether filepath is a package __init__.py."""
    return os.path.basename(str(filepath)) == "__init__.py"


def _resolve_relative_import(imported_name, level, current_module, is_package):
    """Resolve a relative import to an absolute dotted module path.

    Args:
        imported_name: the module/name being imported (may be None for 'from . import x')
        level: number of dots (1 = '.', 2 = '..', etc.)
        current_module: dotted module name of the file doing the import
        is_package: True if current file is __init__.py
    """
    parts = current_module.split(".")
    # For __init__.py the module IS the package; for regular .py go to parent
    if is_package:
        package_parts = list(parts)
    else:
        package_parts = list(parts[:-1])
    up = level - 1
    if up > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - up] if up > 0 else list(package_parts)
    if imported_name:
        base.append(imported_name)
    return ".".join(base) if base else None


def extract_imports_with_context(filepath, root):
    """Extract imports resolving relative imports using file position in project."""
    try:
        source = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, ValueError, OSError):
        return []
    current_module = _filepath_to_module(filepath, root)
    is_pkg = _is_package_init(filepath)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import
                if current_module:
                    if node.module:
                        resolved = _resolve_relative_import(
                            node.module, node.level, current_module, is_pkg
                        )
                        if resolved:
                            imports.append(resolved)
                    else:
                        # from . import x, y, z
                        for alias in node.names:
                            resolved = _resolve_relative_import(
                                alias.name, node.level, current_module, is_pkg
                            )
                            if resolved:
                                imports.append(resolved)
            elif node.module:
                imports.append(node.module)
    return imports


def build_full_dependency_graph(project_root):
    """Scan all .py files and build a forward dependency adjacency list.

    Returns dict[str, set[str]] where each key is a project module and
    the value is the set of project-internal modules it imports.
    Relative imports are resolved to absolute module paths.
    """
    root = str(Path(project_root).resolve())
    py = _py_files(root)

    mod_map = {}  # module_name -> filepath
    for fp in py:
        mod = _filepath_to_module(str(fp), root)
        if mod:
            mod_map[mod] = str(fp)

    graph = defaultdict(set)
    for mod, fp in mod_map.items():
        imported = extract_imports_with_context(fp, root)
        for imp in imported:
            if imp in mod_map:
                graph[mod].add(imp)
            else:
                # Partial match: `import pkg` may correspond to `pkg.__init__`
                for candidate in mod_map:
                    if candidate == imp or candidate.startswith(imp + "."):
                        graph[mod].add(candidate)
        # Ensure every module appears as a key
        if mod not in graph:
            graph[mod] = set()

    return dict(graph)


def get_transitive_dependents(changed_files, graph):
    """BFS reverse walk to find all modules transitively affected by changed_files.

    Builds a reverse graph internally, then does BFS from each changed module.
    Uses a visited set to handle circular dependencies safely.

    Args:
        changed_files: list of module names (dotted) that were changed
        graph: forward dependency graph from build_full_dependency_graph

    Returns:
        set of all affected module names including the changed ones themselves.
    """
    # Build reverse graph: module -> modules that depend on it
    reverse = defaultdict(set)
    for mod, deps in graph.items():
        for dep in deps:
            reverse[dep].add(mod)

    visited = set()
    queue = deque()
    for f in changed_files:
        if f not in visited:
            visited.add(f)
            queue.append(f)

    while queue:
        current = queue.popleft()
        for dependent in reverse.get(current, set()):
            if dependent not in visited:
                visited.add(dependent)
                queue.append(dependent)

    return visited


# ---- Legacy helpers for existing API / tests ----

def build_dependency_graph(root):
    """Build reverse dependency graph and module map.

    Returns (reverse_graph, mod_map) where:
      reverse_graph: dict[str, set[str]] -- module -> modules that depend on it
      mod_map: dict[str, str] -- module_name -> filepath
    """
    root = str(Path(root).resolve())
    py = _py_files(root)

    mod_map = {}
    for fp in py:
        mod = _filepath_to_module(str(fp), root)
        if mod:
            mod_map[mod] = str(fp)

    forward = build_full_dependency_graph(root)

    reverse = defaultdict(set)
    for mod, deps in forward.items():
        for dep in deps:
            reverse[dep].add(mod)

    return dict(reverse), mod_map


def find_affected_tests(changed_files, reverse_graph, mod_map):
    """Given changed file paths, find test files affected via transitive deps."""
    inv_map = {os.path.normpath(v): k for k, v in mod_map.items()}
    root_candidates = set()

    for cf in changed_files:
        norm_cf = os.path.normpath(cf)
        for path, mod in inv_map.items():
            # Exact or suffix match
            if norm_cf == path or norm_cf.endswith(os.sep + os.path.basename(path)):
                root_candidates.add(mod)
        # Also try dotted conversion
        for mod_name in mod_map:
            tail = mod_name.replace(".", os.sep) + ".py"
            init_tail = mod_name.replace(".", os.sep) + os.sep + "__init__.py"
            if cf.endswith(tail) or cf.endswith(init_tail):
                root_candidates.add(mod_name)

    # BFS through reverse graph
    visited = set()
    queue = deque(root_candidates)
    visited.update(root_candidates)
    while queue:
        cur = queue.popleft()
        for dep in reverse_graph.get(cur, set()):
            if dep not in visited:
                visited.add(dep)
                queue.append(dep)

    # Filter to test files
    affected = set()
    for mod in visited:
        fp = mod_map.get(mod, "")
        if _is_test(fp):
            affected.add(fp)
    return affected


def main():
    import argparse

    parser = argparse.ArgumentParser(description="DeltaTest \u2014 CI Test Impact Analysis")
    parser.add_argument("--base", default="main", help="Base branch/commit to diff against")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--json", dest="output_json", action="store_true", help="Output as JSON")
    parser.add_argument("--pytest-args", action="store_true", help="Output as pytest-compatible args")
    args = parser.parse_args()

    changed = get_changed_files(args.base, args.root)
    if not changed:
        if args.output_json:
            print(json.dumps({"changed": [], "affected_tests": []}))
        else:
            print("No Python files changed.")
        return

    reverse_graph, mod_map = build_dependency_graph(args.root)
    affected = find_affected_tests(changed, reverse_graph, mod_map)

    if args.output_json:
        print(json.dumps({"changed": changed, "affected_tests": sorted(affected)}, indent=2))
    elif args.pytest_args:
        print(" ".join(sorted(affected)))
    else:
        print(f"Changed files ({len(changed)}):")
        for f in changed:
            print(f"  {f}")
        print(f"\nAffected tests ({len(affected)}):")
        for t in sorted(affected):
            print(f"  {t}")


if __name__ == "__main__":
    main()
