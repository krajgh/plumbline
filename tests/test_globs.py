"""Glob translation: `**` spans directories, `*` and `?` stay inside a segment."""
import pytest

from plumbline import glob_match, glob_to_regex


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        # **/*.md: zero or more directories, then a name
        ("**/*.md", "README.md", True),
        ("**/*.md", "a/b/c.md", True),
        ("**/*.md", "docs/x.txt", False),
        ("**/*.md", "README.md.bak", False),
        # a trailing ** is everything below, never the directory's siblings
        ("docs/**", "docs/x/y.txt", True),
        ("docs/**", "docs/x.txt", True),
        ("docs/**", "docsx/y", False),
        ("docs/**", "docs", False),
        ("docs/**", "a/docs/x.txt", False),
        # a pattern without a slash matches only from the root
        ("LICENSE", "LICENSE", True),
        ("LICENSE", "docs/LICENSE", False),
        ("LICENSE", "LICENSE.md", False),
        ("pnpm-lock.yaml", "pnpm-lock.yaml", True),
        ("pnpm-lock.yaml", "web/pnpm-lock.yaml", False),
        # written with **/ it matches at any depth, root included
        ("**/Dockerfile", "Dockerfile", True),
        ("**/Dockerfile", "svc/api/Dockerfile", True),
        ("**/.gitignore", ".gitignore", True),
        ("**/.gitignore", "a/.gitignore", True),
        # the tests globs of the default pipeline
        ("tests/**", "tests/test_a.py", True),
        ("tests/**", "tests/deep/er/test_a.py", True),
        ("tests/**", "src/tests/test_a.py", False),
        ("tests/**", "tests", False),
        ("**/test_*.py", "test_a.py", True),
        ("**/test_*.py", "src/pkg/test_a.py", True),
        ("**/test_*.py", "src/a.py", False),
        ("**/test_*.py", "src/test_a.txt", False),
        ("**/*_test.py", "a_test.py", True),
        ("**/*.test.*", "web/a.test.ts", True),
        ("**/*.spec.*", "a.spec.js", True),
        # * and ? never cross a slash
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/pkg/a.py", False),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("a?c", "ac", False),
        # * and ? do match dotfiles: a segment is a segment
        ("**/*.json", ".claude/settings.json", True),
        ("*", ".hidden", True),
        # ** in the middle spans zero or more directories
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "ab", False),
        # ** alone matches every path, and only whole-segment ** is special
        ("**", "a", True),
        ("**", "a/b/c.d", True),
        ("**", ".github/workflows/ci.yml", True),
        ("a**b", "axxb", True),
        ("a**b", "ax/xb", False),
        # everything else is literal
        ("a.b", "a.b", True),
        ("a.b", "axb", False),
        ("[ab]", "[ab]", True),
        ("[ab]", "a", False),
        ("a+b", "a+b", True),
    ],
)
def test_glob_match(pattern, path, expected):
    assert glob_match(pattern, path) is expected


def test_patterns_are_matched_against_the_whole_path():
    assert not glob_match("src", "src/a.py")
    assert not glob_match("a.py", "src/a.py")


def test_a_path_with_a_newline_or_spaces_is_still_one_path():
    assert glob_match("**", "dir with space/f g")
    assert glob_match("*.txt", "odd\nname.txt")


def test_translation_is_cached_and_anchored():
    assert glob_to_regex("docs/**") is glob_to_regex("docs/**")
    assert glob_to_regex("docs/**").pattern.startswith("docs/")
