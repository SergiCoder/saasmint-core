"""Tests for scripts/parse_direct_deps.py — pyproject.toml dependency parser."""

from __future__ import annotations

import pytest

from scripts.parse_direct_deps import parse


@pytest.fixture
def pyproject_file(tmp_path):
    """Return a helper that writes a pyproject.toml and returns its path."""

    def _write(content: str) -> str:
        path = tmp_path / "pyproject.toml"
        path.write_text(content)
        return str(path)

    return _write


class TestParseDirectDeps:
    def test_parses_basic_dependencies(self, pyproject_file):
        path = pyproject_file(
            '[project]\nname = "demo"\n\ndependencies = [\n'
            '    "django>=6.0",\n'
            '    "requests",\n'
            "]\n\n[tool.ruff]\n"
        )
        result = parse(path)
        assert result == ["django", "requests"]

    def test_handles_version_specifiers(self, pyproject_file):
        path = pyproject_file(
            '[project]\nname = "demo"\n\ndependencies = [\n'
            '    "celery[redis]>=5.6.3",\n'
            '    "psycopg[binary]>=3.3.3",\n'
            '    "pydantic>=2.12.5",\n'
            "]\n\n[tool.pytest]\n"
        )
        result = parse(path)
        assert result == ["celery", "psycopg", "pydantic"]

    def test_skips_comments(self, pyproject_file):
        path = pyproject_file(
            '[project]\nname = "demo"\n\ndependencies = [\n'
            "    # this is a comment\n"
            '    "django>=6.0",\n'
            "    # another comment\n"
            "]\n\n[other]\n"
        )
        result = parse(path)
        assert result == ["django"]

    def test_empty_dependencies_list(self, pyproject_file):
        path = pyproject_file('[project]\nname = "demo"\n\ndependencies = [\n]\n\n[other]\n')
        result = parse(path)
        assert result == []

    def test_no_dependencies_section(self, pyproject_file):
        path = pyproject_file('[project]\nname = "demo"\n\n[tool.ruff]\nline-length = 100\n')
        result = parse(path)
        assert result == []

    def test_names_are_lowercased(self, pyproject_file):
        path = pyproject_file(
            '[project]\nname = "demo"\n\ndependencies = [\n'
            '    "Django>=6.0",\n'
            '    "PyJWT>=2.12",\n'
            "]\n\n[other]\n"
        )
        result = parse(path)
        assert result == ["django", "pyjwt"]

    def test_handles_semicolons_and_markers(self, pyproject_file):
        path = pyproject_file(
            '[project]\nname = "demo"\n\ndependencies = [\n'
            "    \"pywin32>=300; sys_platform == 'win32'\",\n"
            "]\n\n[other]\n"
        )
        result = parse(path)
        assert result == ["pywin32"]

    def test_handles_exclamation_version_specifier(self, pyproject_file):
        path = pyproject_file(
            '[project]\nname = "demo"\n\ndependencies = [\n    "urllib3!=2.0.0",\n]\n\n[other]\n'
        )
        result = parse(path)
        assert result == ["urllib3"]

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            parse("/nonexistent/pyproject.toml")

    def test_single_quoted_deps(self, pyproject_file):
        path = pyproject_file(
            "[project]\nname = 'demo'\n\ndependencies = [\n"
            "    'django>=6.0',\n"
            "    'requests',\n"
            "]\n\n[other]\n"
        )
        result = parse(path)
        assert result == ["django", "requests"]

    def test_mixed_specifiers_in_one_file(self, pyproject_file):
        path = pyproject_file(
            '[project]\nname = "demo"\n\ndependencies = [\n'
            '    "django>=6.0.3",\n'
            '    "celery[redis]>=5.6.3",\n'
            '    "urllib3!=2.0.0",\n'
            '    "pydantic>=2.12.5",\n'
            '    "simple-package",\n'
            "]\n\n[other]\n"
        )
        result = parse(path)
        assert result == ["django", "celery", "urllib3", "pydantic", "simple-package"]
