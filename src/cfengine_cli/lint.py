"""
Linting of CFEngine related files.

Currently implemented for:
- *.cf (policy files)
- cfbs.json (CFEngine Build project files)
- *.json (basic JSON syntax checking)

Usage:
$ cfengine lint
"""

import os
import json
import tree_sitter_cfengine as tscfengine
from tree_sitter import Language, Parser
from cfbs.validate import validate_config
from cfbs.cfbs_config import CFBSConfig

DEPRECATED_PROMISE_TYPES = ["defaults", "guest_environments"]
ALLOWED_BUNDLE_TYPES = ["agent", "common", "monitor", "server", "edit_line", "edit_xml"]


def lint_cfbs_json(filename) -> int:
    assert os.path.isfile(filename)
    assert filename.endswith("cfbs.json")

    config = CFBSConfig.get_instance(filename=filename, non_interactive=True)
    r = validate_config(config)

    if r == 0:
        print(f"PASS: {filename}")
        return 0
    print(f"FAIL: {filename}")
    return r


def lint_json(filename) -> int:
    assert os.path.isfile(filename)

    with open(filename, "r") as f:
        data = f.read()

    try:
        data = json.loads(data)
    except:
        print(f"FAIL: {filename} (invalid JSON)")
        return 1
    print(f"PASS: {filename}")
    return 0


def _highlight_range(node, lines):
    line = node.range.start_point[0] + 1
    column = node.range.start_point[1]

    length = len(lines[line - 1]) - column
    if node.range.start_point[0] == node.range.end_point[0]:
        # Starts and ends on same line:
        length = node.range.end_point[1] - node.range.start_point[1]
    assert length >= 1
    print("")
    if line >= 2:
        print(lines[line - 2])
    print(lines[line - 1])
    marker = "^"
    if length > 2:
        marker += "-" * (length - 2)
    if length > 1:
        marker += "^"
    print(" " * column + marker)


def _text(node):
    return node.text.decode()


def _walk_generic(filename, lines, node, visitor):
    visitor(node)
    for node in node.children:
        _walk_generic(filename, lines, node, visitor)


def _find_node_type(filename, lines, node, node_type):
    matches = []
    visitor = lambda x: matches.extend([x] if x.type == node_type else [])
    _walk_generic(filename, lines, node, visitor)
    return matches


def _find_nodes(filename, lines, node):
    matches = []
    visitor = lambda x: matches.append(x)
    _walk_generic(filename, lines, node, visitor)
    return matches


def _single_node_checks(filename, lines, node):
    """Things which can be checked by only looking at one node,
    not needing to recurse into children."""
    line = node.range.start_point[0] + 1
    column = node.range.start_point[1] + 1
    if node.type == "attribute_name" and _text(node) == "ifvarclass":
        _highlight_range(node, lines)
        print(
            f"Deprecation: Use 'if' instead of 'ifvarclass' at {filename}:{line}:{column}"
        )
        return 1
    if node.type == "promise_guard":
        assert _text(node) and len(_text(node)) > 1 and _text(node)[-1] == ":"
        promise_type = _text(node)[0:-1]
        if promise_type in DEPRECATED_PROMISE_TYPES:
            _highlight_range(node, lines)
            print(
                f"Deprecation: Promise type '{promise_type}' is deprecated at {filename}:{line}:{column}"
            )
            return 1
    if node.type == "bundle_block_name":
        if _text(node) != _text(node).lower():
            _highlight_range(node, lines)
            print(
                f"Convention: Bundle name should be lowercase at {filename}:{line}:{column}"
            )
            return 1
    if node.type == "promise_block_name":
        if _text(node) != _text(node).lower():
            _highlight_range(node, lines)
            print(
                f"Convention: Promise type should be lowercase at {filename}:{line}:{column}"
            )
            return 1
    if node.type == "bundle_block_type":
        if _text(node) not in ALLOWED_BUNDLE_TYPES:
            _highlight_range(node, lines)
            print(
                f"Error: Bundle type must be one of ({', '.join(ALLOWED_BUNDLE_TYPES)}), not '{_text(node)}' at {filename}:{line}:{column}"
            )
            return 1
    return 0


def _walk(filename, lines, node) -> int:
    error_nodes = _find_node_type(filename, lines, node, "ERROR")
    if error_nodes:
        for node in error_nodes:
            line = node.range.start_point[0] + 1
            column = node.range.start_point[1] + 1
            _highlight_range(node, lines)
            print(f"Error: Syntax error at {filename}:{line}:{column}")
        return len(error_nodes)

    line = node.range.start_point[0] + 1
    column = node.range.start_point[1] + 1

    errors = 0
    for node in _find_nodes(filename, lines, node):
        errors += _single_node_checks(filename, lines, node)

    return errors


def lint_policy_file(
    filename, original_filename=None, original_line=None, snippet=None, prefix=None
):
    assert original_filename is None or type(original_filename) is str
    assert original_line is None or type(original_line) is int
    assert snippet is None or type(snippet) is int
    if (
        original_filename is not None
        or original_line is not None
        or snippet is not None
    ):
        assert original_filename and os.path.isfile(original_filename)
        assert original_line and original_line > 0
        assert snippet and snippet > 0
    assert os.path.isfile(filename)
    assert filename.endswith((".cf", ".cfengine3", ".cf3", ".cf.sub"))
    PY_LANGUAGE = Language(tscfengine.language())
    parser = Parser(PY_LANGUAGE)

    with open(filename, "rb") as f:
        original_data = f.read()
    tree = parser.parse(original_data)
    lines = original_data.decode().split("\n")

    root_node = tree.root_node
    if root_node.type != "source_file":
        if snippet:
            assert original_filename and original_line
            print(
                f"Error: Failed to parse policy snippet {snippet} at '{original_filename}:{original_line}'"
            )
        else:
            print(f"       Empty policy file '{filename}'")
        print("       Is this valid CFEngine policy?")
        print("")
        lines = original_data.decode().split("\n")
        if not len(lines) <= 5:
            lines = lines[:4] + ["..."]
        for line in lines:
            print("       " + line)
        print("")
        return 1
    assert root_node.type == "source_file"
    errors = 0
    if not root_node.children:
        if snippet:
            assert original_filename and original_line
            print(
                f"Error: Empty policy snippet {snippet} at '{original_filename}:{original_line}'"
            )
        else:
            print(f"Error: Empty policy file '{filename}'")
        errors += 1
    errors += _walk(filename, lines, root_node)
    if prefix:
        print(prefix, end="")
    if errors == 0:
        if snippet:
            assert original_filename and original_line
            print(
                f"PASS: Snippet {snippet} at '{original_filename}:{original_line}' (cf3)"
            )
        else:
            print(f"PASS: {filename}")
        return 0

    if snippet:
        assert original_filename and original_line
        print(f"FAIL: Snippet {snippet} at '{original_filename}:{original_line}' (cf3)")
    else:
        print(f"FAIL: {filename} ({errors} error{'s' if errors > 0 else ''})")
    return errors
