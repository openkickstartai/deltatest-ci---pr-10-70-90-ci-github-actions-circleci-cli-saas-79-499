"""Tests for DeltaTest core engine."""
import os
import tempfile
from deltatest import extract_imports, build_dependency_graph, find_affected_tests, _is_test


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
        os.makedirs(os.path.join(root, "myapp"))
        files = {
            "myapp/__init__.py": "",
            "myapp/core.py": "def add(a, b):\n    return a + b\n",
            "myapp/utils.py": "from myapp.core import add\ndef double(x):\n    return add(x, x)\n",
            "test_core.py": "from myapp.core import add\ndef test_add():\n    assert add(1, 2) == 3\n",
            "test_utils.py": "from myapp.utils import double\ndef test_d():\n    assert double(3) == 6\n",
        }
        for name, content in files.items():
            with open(os.path.join(root, name), "w") as fh:
                fh.write(content)
        reverse, mod_map = build_dependency_graph(root)
        core_deps = reverse.get("myapp/core.py", set())
        assert "test_core.py" in core_deps
        assert "myapp/utils.py" in core_deps
        assert "test_utils.py" in reverse.get("myapp/utils.py", set())
        assert "myapp.core" in mod_map
        assert "myapp.utils" in mod_map


def test_find_affected_tests_transitive():
    """Changing a deep dependency transitively selects downstream tests."""
    reverse = {
        "src/core.py": {"src/utils.py", "test_core.py"},
        "src/utils.py": {"test_utils.py"},
    }
    affected = find_affected_tests(["src/core.py"], reverse)
    assert "test_core.py" in affected
    assert "test_utils.py" in affected


def test_find_affected_tests_isolation():
    """Changing one module does not select unrelated tests."""
    reverse = {"src/a.py": {"test_a.py"}, "src/b.py": {"test_b.py"}}
    affected = find_affected_tests(["src/a.py"], reverse)
    assert "test_a.py" in affected
    assert "test_b.py" not in affected


def test_find_affected_tests_changed_test_file():
    """A changed test file is always included in affected set."""
    affected = find_affected_tests(["test_something.py"], {})
    assert "test_something.py" in affected


def test_is_test_detection():
    """_is_test correctly identifies test file naming patterns."""
    assert _is_test("test_foo.py") is True
    assert _is_test("foo_test.py") is True
    assert _is_test("tests/test_bar.py") is True
    assert _is_test("foo.py") is False
    assert _is_test("testing.py") is False
    assert _is_test("conftest.py") is False
