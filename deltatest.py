#!/usr/bin/env python3
"""DeltaTest - CI Test Impact Analysis Engine. Only run affected tests."""
import ast
import os
import subprocess
import json
from pathlib import Path
from collections import defaultdict
from deltatest_stats import estimate_savings, format_savings_line


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


def build_dependency_graph(root="."):
    """Build reverse dependency graph: source_file -> {files that import it}."""
    rp = Path(root).resolve()
    files = _py_files(root)
    mod_map = {}
    for fp in files:
        parts = list(fp.relative_to(rp).parts)
        if parts[-1] == "__init__.py":
            mod = ".".join(parts[:-1])
        else:
            mod = ".".join(parts)[:-3]  # strip .py
        if mod:
            mod_map[mod] = str(fp.relative_to(rp))
    reverse = defaultdict(set)
    for fp in files:
        rel = str(fp.relative_to(rp))
        for imp in extract_imports(str(fp)):
            candidate = imp
            while candidate:
                if candidate in mod_map:
                    reverse[mod_map[candidate]].add(rel)
                    break
                candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
    return dict(reverse), mod_map


def find_affected_tests(changed_files, reverse_deps):
    """BFS through reverse deps to find all test files affected by changes."""
    affected = set()
    visited = set()
    queue = list(changed_files)
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        if _is_test(current):
            affected.add(current)
        for dep in reverse_deps.get(current, []):
            if dep not in visited:
                queue.append(dep)
    return sorted(affected)


def analyze(base="main", root="."):
    """Full analysis pipeline: git diff -> dep graph -> affected tests."""
    rp = Path(root).resolve()
    changed = get_changed_files(base, str(rp))
    rev, _ = build_dependency_graph(str(rp))
    all_tests = sorted(str(p.relative_to(rp)) for p in _py_files(str(rp)) if _is_test(p.name))
    affected = find_affected_tests(changed, rev)
    total, selected = len(all_tests), len(affected)
    pct = round((total - selected) / total * 100, 1) if total else 0.0
    return {
        "changed_files": changed, "total_tests": total,
        "selected_tests": selected, "skipped_tests": total - selected,
        "skip_percentage": pct, "affected_test_files": affected,
        "skipped_test_files": [t for t in all_tests if t not in set(affected)],
    }


def main():
    import argparse
    p = argparse.ArgumentParser(description="DeltaTest - CI Test Impact Analysis")
    p.add_argument("--base", default="main", help="Base branch (default: main)")
    p.add_argument("--root", default=".", help="Project root directory")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--pytest-args", action="store_true", help="Pytest-compatible file list")
    args = p.parse_args()
    result = analyze(args.base, args.root)
    if args.pytest_args:
        print(" ".join(result["affected_test_files"]))
    elif args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\U0001f52c DeltaTest Impact Analysis\n{'=' * 42}")
        print(f"Changed:  {len(result['changed_files'])} files")
        print(f"Tests:    {result['selected_tests']}/{result['total_tests']} selected")
        print(f"Skipped:  {result['skipped_tests']} ({result['skip_percentage']}%)")
        if result["affected_test_files"]:
            print("\nRun these tests:")
            for t in result["affected_test_files"]:
                print(f"  \u2705 {t}")
        if result["skipped_test_files"]:
            print("\nSafely skipped:")
            for t in result["skipped_test_files"]:
                print(f"  \u23ed\ufe0f  {t}")
        est = result["skipped_tests"] * 0.5 * 20 * 22
        print(f"\n\U0001f4b0 Est. monthly CI savings: ~{int(est)} minutes")


if __name__ == "__main__":
    main()
