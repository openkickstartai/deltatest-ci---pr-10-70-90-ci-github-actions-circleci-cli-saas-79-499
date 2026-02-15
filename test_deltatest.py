"""Tests for DeltaTest core engine."""
import os
import tempfile
from deltatest import (
    extract_imports,
    extract_imports_with_context,
    build_dependency_graph,
    build_full_dependency_graph,
    get_transitive_dependents,
    find_affected_tests,
    _is_test,
    _filepath_to_module,
    _resolve_relative_import,
)


def _make_project(root, files):
    """Helper: write a dict of {relative_path: content} into root."""
    for name, content in files.items():
        fp = os.path.join(root, name)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as fh:
            fh.write(content)


# ===== Existing tests =====


def test_extract_imports_basic():
    """Verify that import and from-import statements are extracted."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("import os\nimport json\nfrom pathlib import Path\nfrom myapp.utils import helper\n")
        f.flush()
        result = extract_imports(f.name)
    os.unlink(f.name)
    assert "os" in result
    assert "json" in result
    assert "pathlib" in result
    assert "myapp.utils" in result
    assert len(result) == 4


def test_extract_imports_handles_syntax_error():
    """Files with syntax errors return empty list, no crash."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("def broken(\n  # unterminated")
        f.flush()
        result = extract_imports(f.name)
    os.unlink(f.name)
    assert result == []


def test_build_dependency_graph_resolves_imports():
    """Dependency graph correctly maps source files to their importers."""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, {
            "myapp/__init__.py": "",
            "myapp/core.py": "def add(a, b):\n    return a + b\n",
            "myapp/utils.py": "from myapp.core import add\ndef double(x):\n    return add(x, x)\n",
            "test_core.py": "from myapp.core import add\ndef test_add():\n    assert add(1, 2) == 3\n",
            "test_utils.py": "from myapp.utils import double\ndef test_d():\n    assert double(3) == 6\n",
        })
        reverse, mod_map = build_dependency_graph(root)
        # myapp.core is imported by myapp.utils and test_core
        assert "myapp.utils" in reverse.get("myapp.core", set())
        assert "test_core" in reverse.get("myapp.core", set())
        # myapp.utils is imported by test_utils
        assert "test_utils" in reverse.get("myapp.utils", set())


def test_is_test():
    assert _is_test("test_foo.py")
    assert _is_test("bar_test.py")
    assert not _is_test("foo.py")
    assert not _is_test("conftest.py")


# ===== NEW: multi-layer transitive dependency =====


def test_transitive_multi_layer_dependency():
    """A -> B -> C: changing C must flag A and test_a as affected."""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, {
            "pkg/__init__.py": "",
            "pkg/c.py": "VALUE = 42\n",
            "pkg/b.py": "from pkg.c import VALUE\ndef get(): return VALUE\n",
            "pkg/a.py": "from pkg.b import get\ndef run(): return get()\n",
            "test_a.py": "from pkg.a import run\ndef test_run(): assert run() == 42\n",
        })
        graph = build_full_dependency_graph(root)

        # Forward edges exist
        assert "pkg.a" in graph.get("test_a", set())
        assert "pkg.b" in graph.get("pkg.a", set())
        assert "pkg.c" in graph.get("pkg.b", set())

        # Transitive dependents of pkg.c should include everything up the chain
        affected = get_transitive_dependents(["pkg.c"], graph)
        assert "pkg.c" in affected
        assert "pkg.b" in affected
        assert "pkg.a" in affected
        assert "test_a" in affected


# ===== NEW: circular import handling =====


def test_cycle_detection_no_infinite_loop():
    """Circular imports must not cause infinite loop; all cycle nodes are returned."""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, {
            "cyc/__init__.py": "",
            "cyc/x.py": "from cyc.y import Y\nX = 1\n",
            "cyc/y.py": "from cyc.x import X\nY = 2\n",
            "test_cyc.py": "from cyc.x import X\ndef test_x(): assert X == 1\n",
        })
        graph = build_full_dependency_graph(root)

        # Both directions of the cycle exist
        assert "cyc.y" in graph.get("cyc.x", set())
        assert "cyc.x" in graph.get("cyc.y", set())

        # Changing cyc.y should reach cyc.x and test_cyc without hanging
        affected = get_transitive_dependents(["cyc.y"], graph)
        assert "cyc.y" in affected
        assert "cyc.x" in affected
        assert "test_cyc" in affected


# ===== NEW: relative import resolution =====


def test_relative_import_resolution():
    """Relative imports (from . and from ..) are resolved to correct modules."""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, {
            "mypkg/__init__.py": "",
            "mypkg/core.py": "CORE = True\n",
            "mypkg/helpers.py": "from .core import CORE\nHELPER = CORE\n",
            "mypkg/sub/__init__.py": "",
            "mypkg/sub/deep.py": "from ..core import CORE\nDEEP = CORE\n",
            "test_deep.py": "from mypkg.sub.deep import DEEP\ndef test_deep(): assert DEEP\n",
        })
        graph = build_full_dependency_graph(root)

        # mypkg.helpers depends on mypkg.core via 'from .core'
        assert "mypkg.core" in graph.get("mypkg.helpers", set())

        # mypkg.sub.deep depends on mypkg.core via 'from ..core'
        assert "mypkg.core" in graph.get("mypkg.sub.deep", set())

        # Changing mypkg.core should transitively affect everything
        affected = get_transitive_dependents(["mypkg.core"], graph)
        assert "mypkg.helpers" in affected
        assert "mypkg.sub.deep" in affected
        assert "test_deep" in affected


def test_relative_import_from_dot_import():
    """'from . import x' style relative imports resolve correctly."""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, {
            "rpkg/__init__.py": "",
            "rpkg/alpha.py": "A = 1\n",
            "rpkg/beta.py": "from . import alpha\nB = alpha.A\n",
        })
        graph = build_full_dependency_graph(root)
        assert "rpkg.alpha" in graph.get("rpkg.beta", set())


def test_get_transitive_dependents_multiple_changed():
    """Multiple changed files expand the affected set correctly."""
    graph = {
        "a": {"b"},
        "b": {"c"},
        "c": set(),
        "d": {"c"},
        "e": {"d"},
    }
    # Changing c: reverse path c <- b <- a, c <- d <- e
    affected = get_transitive_dependents(["c"], graph)
    assert affected == {"a", "b", "c", "d", "e"}

    # Changing both b and d
    affected2 = get_transitive_dependents(["b", "d"], graph)
    assert "a" in affected2
    assert "e" in affected2
    assert "b" in affected2
    assert "d" in affected2
