"""Tests for DeltaTest core engine."""
import os
import json
import tempfile
from deltatest import (
    extract_imports,
    build_dependency_graph,
    find_affected_tests,
    _is_test,
    format_output_json,
    format_output_sarif,
)


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
        assert "myapp.core" in mod_map
        assert "myapp.utils" in mod_map
        # core.py should have dependents (test_core.py and utils.py)
        assert "myapp/core.py" in reverse
        assert len(reverse["myapp/core.py"]) >= 2


def test_find_affected_tests_direct():
    """Changing a source file finds direct test dependents."""
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "myapp"))
        files = {
            "myapp/__init__.py": "",
            "myapp/core.py": "def add(a, b):\n    return a + b\n",
            "test_core.py": "from myapp.core import add\ndef test_add():\n    assert add(1, 2) == 3\n",
        }
        for name, content in files.items():
            with open(os.path.join(root, name), "w") as fh:
                fh.write(content)
        reverse, mod_map = build_dependency_graph(root)
        affected = find_affected_tests(["myapp/core.py"], reverse, mod_map, root)
        assert any("test_core" in t for t in affected)


def test_find_affected_tests_transitive():
    """Transitive deps: changing core.py affects test_utils.py via utils.py."""
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "myapp"))
        files = {
            "myapp/__init__.py": "",
            "myapp/core.py": "def add(a, b):\n    return a + b\n",
            "myapp/utils.py": "from myapp.core import add\ndef double(x):\n    return add(x, x)\n",
            "test_utils.py": "from myapp.utils import double\ndef test_d():\n    assert double(3) == 6\n",
        }
        for name, content in files.items():
            with open(os.path.join(root, name), "w") as fh:
                fh.write(content)
        reverse, mod_map = build_dependency_graph(root)
        affected = find_affected_tests(["myapp/core.py"], reverse, mod_map, root)
        assert any("test_utils" in t for t in affected)


def test_is_test():
    """Test file detection works for standard naming conventions."""
    assert _is_test("test_foo.py")
    assert _is_test("foo_test.py")
    assert _is_test("/some/path/test_bar.py")
    assert not _is_test("foo.py")
    assert not _is_test("conftest.py")
    assert not _is_test("testing_utils.py")


def test_json_output_valid():
    """--output-format json produces valid JSON with all required keys."""
    changed = ["app.py", "utils.py"]
    affected = ["test_app.py"]
    all_tests = ["test_app.py", "test_other.py", "test_utils.py", "test_extra.py"]

    output = format_output_json(changed, affected, all_tests)
    data = json.loads(output)  # Must not raise

    assert isinstance(data, dict)
    assert data["changed_files"] == ["app.py", "utils.py"]
    assert data["affected_tests"] == ["test_app.py"]
    assert sorted(data["skipped_tests"]) == ["test_extra.py", "test_other.py", "test_utils.py"]
    assert data["reduction_percent"] == 75.0


def test_json_output_empty():
    """JSON output handles zero tests gracefully."""
    output = format_output_json([], [], [])
    data = json.loads(output)
    assert data["changed_files"] == []
    assert data["affected_tests"] == []
    assert data["skipped_tests"] == []
    assert data["reduction_percent"] == 0


def test_json_reduction_percent_calculation():
    """Verify reduction_percent: 1 affected out of 8 total = 87.5%."""
    all_tests = [f"test_{i}.py" for i in range(8)]
    affected = ["test_0.py"]
    output = format_output_json(["lib.py"], affected, all_tests)
    data = json.loads(output)
    assert data["reduction_percent"] == 87.5


def test_sarif_output_valid():
    """--output-format sarif produces valid SARIF 2.1.0 JSON."""
    changed = ["app.py"]
    affected = ["test_app.py", "test_integration.py"]
    all_tests = ["test_app.py", "test_integration.py", "test_other.py"]

    output = format_output_sarif(changed, affected, all_tests)
    data = json.loads(output)  # Must not raise

    assert data["version"] == "2.1.0"
    assert "$schema" in data
    assert len(data["runs"]) == 1

    run = data["runs"][0]
    assert run["tool"]["driver"]["name"] == "DeltaTest"
    assert len(run["results"]) == 2
    assert run["results"][0]["ruleId"] == "deltatest/affected-test"
    assert run["results"][0]["level"] == "note"
    assert "test_app.py" in run["results"][0]["message"]["text"]


def test_sarif_output_empty():
    """SARIF output with no affected tests has empty results."""
    output = format_output_sarif([], [], [])
    data = json.loads(output)
    assert data["version"] == "2.1.0"
    assert data["runs"][0]["results"] == []


def test_json_all_affected():
    """When all tests are affected, reduction is 0%."""
    all_tests = ["test_a.py", "test_b.py"]
    output = format_output_json(["core.py"], all_tests, all_tests)
    data = json.loads(output)
    assert data["reduction_percent"] == 0
    assert data["skipped_tests"] == []
