#!/usr/bin/env python3
"""DeltaTest - CI Test Impact Analysis Engine. Only run affected tests."""
import ast
import os
import sys
import subprocess
import json
import argparse
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
    """Parse Python AST to extract all imported module names."""
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


def _path_to_module(filepath, root):
    """Convert a file path to a dotted module name."""
    try:
        rel = Path(filepath).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].replace(".py", "")
    return ".".join(parts) if parts else None


def build_dependency_graph(root="."):
    """Build reverse dependency graph: file -> set of files that import it.

    Returns (reverse_deps, mod_map) where mod_map maps dotted module names
    to their relative file paths.
    """
    files = _py_files(root)
    root_resolved = Path(root).resolve()

    mod_to_file = {}
    for f in files:
        mod = _path_to_module(f, root)
        if mod:
            rel = str(f.relative_to(root_resolved))
            mod_to_file[mod] = rel

    reverse = defaultdict(set)
    for f in files:
        rel = str(f.relative_to(root_resolved))
        imports = extract_imports(str(f))
        for imp in imports:
            for mod_name, mod_file in mod_to_file.items():
                if imp == mod_name or imp.startswith(mod_name + ".") or mod_name.startswith(imp + "."):
                    if mod_file != rel:
                        reverse[mod_file].add(rel)

    return dict(reverse), mod_to_file


def find_affected_tests(changed_files, reverse_deps, mod_map, root="."):
    """BFS through reverse deps to find all affected test files."""
    affected = set()
    visited = set()
    queue = deque(changed_files)

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)

        if _is_test(current):
            affected.add(current)

        for dep_file in reverse_deps.get(current, set()):
            if dep_file not in visited:
                queue.append(dep_file)

    return sorted(affected)


def _all_test_files(root):
    """Return relative paths of all test files under root."""
    root_resolved = Path(root).resolve()
    return sorted(
        str(f.relative_to(root_resolved))
        for f in _py_files(root)
        if _is_test(str(f))
    )


def format_output_json(changed_files, affected_tests, all_test_files):
    """Format analysis results as JSON.

    Output schema:
        changed_files: list[str]
        affected_tests: list[str]
        skipped_tests: list[str]
        reduction_percent: float
    """
    skipped = sorted(set(all_test_files) - set(affected_tests))
    total = len(all_test_files)
    reduction = round(((total - len(affected_tests)) / total * 100) if total > 0 else 0, 1)
    return json.dumps({
        "changed_files": changed_files,
        "affected_tests": affected_tests,
        "skipped_tests": skipped,
        "reduction_percent": reduction,
    }, indent=2)


def format_output_sarif(changed_files, affected_tests, all_test_files):
    """Format analysis results as SARIF 2.1.0."""
    results = []
    for t in affected_tests:
        results.append({
            "ruleId": "deltatest/affected-test",
            "level": "note",
            "message": {"text": f"Test file {t} is affected by changes"},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": t}
                }
            }],
        })

    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "DeltaTest",
                    "version": "0.1.0",
                    "informationUri": "https://github.com/deltatest/deltatest",
                    "rules": [{
                        "id": "deltatest/affected-test",
                        "shortDescription": {"text": "Test affected by code changes"},
                    }],
                }
            },
            "results": results,
        }],
    }
    return json.dumps(sarif, indent=2)


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="deltatest",
        description="DeltaTest — CI Test Impact Analysis Engine",
    )
    parser.add_argument("--base", default="main", help="Base branch to diff against (default: main)")
    parser.add_argument("--root", default=".", help="Project root directory (default: .)")
    parser.add_argument("--pytest-args", action="store_true", help="Output affected tests as pytest arguments")
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "sarif"],
        default="text",
        help="Output format: text (default), json, or sarif",
    )
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    changed = get_changed_files(args.base, root)
    reverse_deps, mod_map = build_dependency_graph(root)
    affected = find_affected_tests(changed, reverse_deps, mod_map, root)
    all_tests = _all_test_files(root)

    if args.output_format == "json":
        print(format_output_json(changed, affected, all_tests))
    elif args.output_format == "sarif":
        print(format_output_sarif(changed, affected, all_tests))
    elif args.pytest_args:
        print(" ".join(affected))
    else:
        total = len(all_tests)
        skipped = total - len(affected)
        reduction = round((skipped / total * 100) if total > 0 else 0, 1)
        print(f"DeltaTest Impact Analysis (base: {args.base})")
        print(f"  Changed files:  {len(changed)}")
        print(f"  Affected tests: {len(affected)} / {total}")
        print(f"  Skipped tests:  {skipped}")
        print(f"  Reduction:      {reduction}%")
        if affected:
            print("\nAffected test files:")
            for t in affected:
                print(f"  \u2022 {t}")


if __name__ == "__main__":
    main()
