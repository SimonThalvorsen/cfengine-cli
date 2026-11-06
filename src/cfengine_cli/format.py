from __future__ import annotations

from enum import IntEnum
from typing import IO

import json
import os
import sys

import tree_sitter_cfengine as tscfengine
from tree_sitter import Language, Parser, Node
from cfbs.pretty import pretty_file, pretty_check_file
from cfengine_cli.lint import check_policy_syntax, PolicySyntaxError
from cfengine_cli.utils import UserError

# Node types that increase indentation by 2 when entered
INDENTED_TYPES = {
    "bundle_section",
    "class_guarded_promises",
    "class_guarded_body_attributes",
    "class_guarded_promise_block_attributes",
    "promise",
    "half_promise",
    "attribute",
}

CLASS_GUARD_TYPES = {
    "class_guarded_promises",
    "class_guarded_body_attributes",
    "class_guarded_promise_block_attributes",
}

BLOCK_TYPES = {"bundle_block", "promise_block", "body_block"}

BLOCK_BODY_TYPES = {"bundle_block_body", "promise_block_body", "body_block_body"}

PROMISER_PARTS = {"promiser", "->", "stakeholder"}

SAMELINE_COMMENT_SEPARATOR = " "


class FormatResult(IntEnum):
    """Outcome of a formatting operation."""

    NO_CHANGE = 0  # File was already formatted, no reformat needed
    NEEDS_FORMAT = 1  # Reformat needed (reported in check mode)
    REFORMATTED = 2  # File was reformatted successfully


def _has_direct_macro(node: Node) -> bool:
    """Check if any direct child of a node is a macro (non-recursive)."""
    return any(child.type == "macro" for child in node.children)


def _contains_macro(nodes: Node | list[Node]) -> bool:
    """Check if a node (or any node in a list) is or contains a macro (recursive)."""
    if isinstance(nodes, list):
        return any(_contains_macro(node) for node in nodes)
    if nodes.type == "macro":
        return True
    return _contains_macro(nodes.children)


def _contains_list_with_comment(nodes: Node | list[Node]) -> bool:
    """Check if a node (or list) contains a list with a comment child.

    A `#` comment extends to end of line, so a list containing one cannot
    be rendered on a single line — every such list must be split."""
    if isinstance(nodes, list):
        return any(_contains_list_with_comment(node) for node in nodes)
    if nodes.type == "list" and any(c.type == "comment" for c in nodes.children):
        return True
    return _contains_list_with_comment(nodes.children)


def format_json_file(filename: str, check: bool) -> FormatResult:
    """Reformat a JSON file in place using cfbs pretty-printer."""
    assert filename.endswith(".json")

    if check:
        success = pretty_check_file(filename)
        # pretty_check_file() in cfbs needs correct typehint:
        assert type(success) is bool
        if not success:
            print(f"JSON file '{filename}' needs reformatting")
            return FormatResult.NEEDS_FORMAT
        return FormatResult.NO_CHANGE

    try:
        reformatted = pretty_file(filename)
        if reformatted:
            print(f"JSON file '{filename}' was reformatted")
            return FormatResult.REFORMATTED
        return FormatResult.NO_CHANGE
    except json.decoder.JSONDecodeError as e:
        raise UserError(f"JSON file '{filename}' invalid ({str(e)})")


def text(node: Node) -> str:
    """Extract the UTF-8 text content of a tree-sitter node."""
    if not node.text:
        return ""
    return node.text.decode("utf-8")


class Formatter:
    """Accumulates formatted output line-by-line into a string buffer."""

    def __init__(self) -> None:
        self.empty: bool = True
        self.previous: Node | None = None
        self.buffer: str = ""
        # Indentation level saved when @if is encountered, restored after
        # @else so that both branches are formatted at the same depth.
        self.macro_indent: int = 0

    def _write(self, message: str, end: str = "\n") -> None:
        """Append text to the buffer with the given line ending."""
        self.buffer += message + end

    def print_lines(self, lines: list[str], indent: int) -> None:
        """Print multiple pre-formatted lines."""
        for line in lines:
            self.print(line, indent)

    def print(self, string: str | Node, indent: int) -> None:
        """Print a string or node on a new line with the given indentation."""
        if not isinstance(string, str):
            string = text(string)
        if not self.empty:
            self._write("\n", end="")
        self._write(" " * indent + string, end="")
        self.empty = False

    def print_same_line(self, string: str | Node) -> None:
        """Append text to the current line without a preceding newline."""
        if not isinstance(string, str):
            string = text(string)
        self._write(string, end="")

    def blank_line(self) -> None:
        """Insert a blank separator line."""
        self.print("", 0)

    def update_previous(self, node: Node) -> Node | None:
        """Set the previously-visited node, returning the old value."""
        tmp = self.previous
        self.previous = node
        return tmp


# ---------------------------------------------------------------------------
# Stringify helpers — flatten nodes into single-line strings
# ---------------------------------------------------------------------------


def stringify_parameter_list(parts: list[str]) -> str:
    """Join string tokens into a formatted parameter list.

    Removes trailing commas and adds spacing after commas.
    Example: ["(", "a", ",", "b", ",", ")"] -> "(a, b)"
    """
    # Remove trailing comma before closing paren
    cleaned = []
    for i, part in enumerate(parts):
        if part == "," and i + 1 < len(parts) and parts[i + 1] == ")":
            continue
        cleaned.append(part)
    result = ""
    previous = None
    for part in cleaned:
        if previous and previous != "(" and part != "," and part != ")":
            result += " "
        elif previous == ",":
            result += " "
        result += part
        previous = part
    return result


def stringify_single_line_nodes(nodes: list[Node]) -> str:
    """Join tree-sitter nodes into a single-line string with CFEngine spacing.

    Inserts spaces after ",", around "=>", and inside "{ }".
    Strips trailing commas immediately preceding ")" or "}".
    """
    result = ""
    previous = None
    for i, node in enumerate(nodes):
        # Strip trailing comma before closing bracket/paren
        if node.type == ",":
            next_node = nodes[i + 1] if i + 1 < len(nodes) else None
            if next_node is not None and next_node.type in (")", "}"):
                continue
        string = stringify_single_line_node(node)
        if previous and previous.type == ",":
            result += " "
        if previous and node.type == "=>":
            result += " "
        if previous and previous.type == "=>":
            result += " "
        if previous and previous.type == "{" and node.type != "}":
            result += " "
        if previous and node.type == "}" and previous.type != "{":
            result += " "
        result += string
        previous = node
    return result


def stringify_single_line_node(node: Node) -> str:
    """Recursively flatten a node and its children into a single-line string."""
    if not node.children:
        return text(node)
    return stringify_single_line_nodes(node.children)


# ---------------------------------------------------------------------------
# List / rval splitting — multi-line formatting for long values
# ---------------------------------------------------------------------------


def split_generic_value(node: Node, indent: int, line_length: int) -> list[str]:
    """Split a value node (call, list, or other) into multi-line strings."""
    if node.type == "call":
        return split_rval_call(node, indent, line_length)
    if node.type == "list":
        return split_rval_list(node, indent, line_length)
    return [stringify_single_line_node(node)]


def _set_trailing_comma(line: str, add: bool) -> str:
    """Add or remove a trailing comma from a formatted line."""
    if add and not line.endswith(","):
        return line + ","
    if not add and line.endswith(","):
        return line[:-1]
    return line


def split_generic_list(
    middle: list[Node], indent: int, line_length: int, trailing_comma: bool = True
) -> list[str]:
    """Split list elements into one-per-line strings, each pre-indented."""
    has_macros = _contains_macro(middle)
    elements: list[str] = []
    # Indices of the lines that end a top-level value element. For a nested
    # element, like a function call inside an slist, this is only the final line,
    # outside the function call, so we do not add trailing commas into function
    # call argument lists.
    value_end_indices: list[int] = []
    # Same-line (trailing) comments are attached only after trailing commas are
    # placed, so the comma lands before the comment instead of after it.
    sameline_comments: dict[int, str] = {}
    for element in middle:
        if elements and element.type == ",":
            elements[-1] = elements[-1] + ","
            continue
        if element.type == "macro":
            elements.append(text(element))
            continue
        if element.type == "comment":
            if _is_sameline_comment(element) and elements:
                sameline_comments[len(elements) - 1] = text(element)
            else:
                elements.append(" " * indent + text(element))
            continue
        line = " " * indent + stringify_single_line_node(element)
        # Strict < reserves 1 char for the comma appended after this check
        if len(line) < line_length:
            elements.append(line)
        else:
            lines = split_generic_value(element, indent, line_length)
            elements.append(" " * indent + lines[0])
            elements.extend(lines[1:])
        value_end_indices.append(len(elements) - 1)

    # Set trailing commas
    if has_macros:
        # With macros: Use the indices we counted earlier to ensure there
        # are trailing commas at the right places (before macro, inside macro,
        # after macros), but NOT inside the nested function calls which may
        # be multi line
        for i in value_end_indices:
            elements[i] = _set_trailing_comma(elements[i], trailing_comma)
    else:
        # No macros: Just ensure the last non-comment element has a trailing comma
        for i in reversed(range(len(elements))):
            if not elements[i].lstrip().startswith("#"):
                elements[i] = _set_trailing_comma(elements[i], trailing_comma)
                break

    # Attach same-line comments to the end of their value lines
    for i, comment in sameline_comments.items():
        elements[i] = elements[i] + SAMELINE_COMMENT_SEPARATOR + comment
    return elements


def maybe_split_generic_list(
    nodes: list[Node], indent: int, line_length: int, trailing_comma: bool = True
) -> list[str]:
    """Try a single-line rendering; fall back to split_generic_list if too long."""
    has_comment = any(n.type == "comment" for n in nodes)
    if not _contains_macro(nodes) and not has_comment:
        string = " " * indent + stringify_single_line_nodes(nodes)
        if len(string) <= line_length:
            return [string]
    return split_generic_list(nodes, indent, line_length, trailing_comma)


def split_rval_list(node: Node, indent: int, line_length: int) -> list[str]:
    """Split a list rval ({ ... }) into multi-line form."""
    assert node.type == "list"
    assert node.children[0].type == "{"
    first = text(node.children[0])
    last = " " * indent + text(node.children[-1])
    middle = node.children[1:-1]
    elements = maybe_split_generic_list(middle, indent + 2, line_length)
    return [first, *elements, last]


def split_rval_call(node: Node, indent: int, line_length: int) -> list[str]:
    """Split a function call rval (func(...)) into multi-line form."""
    assert node.type == "call"
    assert node.children[0].type == "calling_identifier"
    assert node.children[1].type == "("
    first = text(node.children[0]) + "("
    last = " " * indent + text(node.children[-1])
    middle = node.children[2:-1]
    elements = maybe_split_generic_list(
        middle, indent + 2, line_length, trailing_comma=False
    )
    return [first, *elements, last]


def split_rval(node: Node, indent: int, line_length: int) -> list[str]:
    """Split an rval node into multi-line form based on its type."""
    if node.type == "list":
        return split_rval_list(node, indent, line_length)
    if node.type == "call":
        return split_rval_call(node, indent, line_length)
    return [stringify_single_line_node(node)]


def maybe_split_rval(
    node: Node, indent: int, offset: int, line_length: int
) -> list[str]:
    """Return single-line rval if it fits at offset, otherwise split it."""
    if _contains_macro(node) or _contains_list_with_comment(node):
        return split_rval(node, indent, line_length)
    line = stringify_single_line_node(node)
    if len(line) + offset <= line_length:
        return [line]
    return split_rval(node, indent, line_length)


# ---------------------------------------------------------------------------
# Attribute formatting
# ---------------------------------------------------------------------------


def _format_attribute_with_macros(node: Node, indent: int) -> list[str]:
    """Format an attribute whose direct children include macro nodes."""
    lines: list[str] = []
    children = node.children

    lval = children[0]
    arrow = children[1]
    lines.append(" " * indent + text(lval) + " " + text(arrow))

    for child in children[2:]:
        if child.type == "macro":
            lines.append(text(child))
        elif child.type == "comment":
            if _is_sameline_comment(child) and lines:
                lines[-1] = lines[-1] + SAMELINE_COMMENT_SEPARATOR + text(child)
            else:
                lines.append(" " * (indent + 2) + text(child))
        else:
            lines.append(" " * (indent + 2) + stringify_single_line_node(child))

    return lines


def _attempt_split_attribute(node: Node, indent: int, line_length: int) -> list[str]:
    """Split an attribute node, wrapping the rval if it's a list or call."""
    # When macros appear as direct children of the attribute, use
    # the dedicated macro-aware formatter.
    if _has_direct_macro(node):
        return _format_attribute_with_macros(node, indent)

    assert len(node.children) >= 3  # lval + arrow + rval + optionally comments

    # Separate comments from the 3 structural children (lval, arrow, rval).
    # Comments are moved to appear before the attribute.
    children = []
    comments = []
    for child in node.children:
        if child.type == "comment":
            comments.append(child)
        else:
            children.append(child)

    assert len(children) == 3

    lval = children[0]
    arrow = children[1]
    rval = children[2]

    comment_lines = [" " * indent + text(c) for c in comments]

    if rval.type == "list" or rval.type == "call":
        prefix = " " * indent + text(lval) + " " + text(arrow) + " "
        offset = len(prefix)
        lines = maybe_split_rval(rval, indent, offset, line_length)
        lines[0] = prefix + lines[0]
        return comment_lines + lines
    return comment_lines + [" " * indent + stringify_single_line_node(node)]


def _stringify(node: Node, indent: int, line_length: int) -> list[str]:
    """Return a node as pre-indented line(s), splitting if it exceeds line_length."""
    # Attributes containing macros must always be split — macros cannot
    # appear inline on a single line. Same for lists with comments inside,
    # since `#` extends to end of line.
    if node.type == "attribute" and (
        _contains_macro(node) or _contains_list_with_comment(node)
    ):
        return _attempt_split_attribute(node, indent, line_length - 1)
    single_line = " " * indent + stringify_single_line_node(node)
    # Reserve 1 char for trailing ; or , after attributes
    effective_length = line_length - 1 if node.type == "attribute" else line_length
    if len(single_line) <= effective_length:
        return [single_line]
    if node.type == "attribute":
        return _attempt_split_attribute(node, indent, line_length - 1)
    return [single_line]


# ---------------------------------------------------------------------------
# Stakeholder helpers
# ---------------------------------------------------------------------------


def _get_stakeholder_list(children: list[Node]) -> Node | None:
    """Return the list node inside a promise's stakeholder, or None."""
    stakeholder = next((c for c in children if c.type == "stakeholder"), None)
    if not stakeholder:
        return None
    return next((c for c in stakeholder.children if c.type == "list"), None)


def _stakeholder_has_comments(children: list[Node]) -> bool:
    """Check if the stakeholder list contains any comment nodes."""
    list_node = _get_stakeholder_list(children)
    if not list_node:
        return False
    return any(c.type == "comment" for c in list_node.children)


def _has_trailing_comma(middle: list[Node]) -> bool:
    """Check if a list's middle nodes end with a trailing comma.

    middle (the parameter name) refers to the middle of a list of nodes,
    since the list started and ended with parentheses / curly braces.
    Those "ends" have been removed, leaving the middle.

    For example:
    nodes = ["{", "a", ",", "b", "}"]
    middle = nodes[1:-1]
    """
    for node in reversed(middle):
        if node.type == "comment":
            continue  # Skip comments
        if node.type == ",":
            return True  # Found comma, list has trailing comma
        return False  # Found something else, list does not have trailing comma
    return False


def _promiser_text(children: list[Node]) -> str | None:
    """Return the raw promiser string from promise children, or None."""
    promiser_node = next((c for c in children if c.type == "promiser"), None)
    if not promiser_node:
        return None
    return text(promiser_node)


def _promiser_line_with_stakeholder(children: list[Node]) -> str | None:
    """Build the full promiser line including '-> { stakeholder }', or None."""
    prefix = _promiser_text(children)
    if not prefix:
        return None
    arrow = next((c for c in children if c.type == "->"), None)
    stakeholder = next((c for c in children if c.type == "stakeholder"), None)
    if arrow and stakeholder:
        prefix += " " + text(arrow) + " " + stringify_single_line_node(stakeholder)
    return prefix


def _stakeholder_needs_splitting(
    children: list[Node], indent: int, line_length: int
) -> bool:
    """Check if the stakeholder list must be split (comments or too long).

    This function is only used on promises without attributes, since
    promises with both attributes and stakeholder are always split."""
    if _stakeholder_has_comments(children):
        return True
    line = _promiser_line_with_stakeholder(children)
    assert line
    return indent + len(line) > line_length


def _format_stakeholder_elements(
    middle: list[Node], indent: int, line_length: int
) -> list[str]:
    """Format the elements between { and } of a stakeholder list.

    Uses trailing-comma heuristic: lists with a trailing comma are always
    split one-per-line; without, they may stay on a single line.
    """
    if not any(n.type == "comment" for n in middle):
        if _has_trailing_comma(middle):
            return split_generic_list(middle, indent, line_length)
        return maybe_split_generic_list(middle, indent, line_length)
    # Comments present — format element-by-element to preserve them
    elements: list[str] = []
    # Same-line (trailing) comments are attached only after the trailing comma is
    # placed, so the comma lands before the comment instead of after it.
    sameline_comments: dict[int, str] = {}
    for node in middle:
        if node.type == ",":
            if elements:
                elements[-1] = elements[-1] + ","
            continue
        if node.type == "comment":
            if _is_sameline_comment(node) and elements:
                sameline_comments[len(elements) - 1] = text(node)
            else:
                elements.append(" " * indent + text(node))
        else:
            line = " " * indent + stringify_single_line_node(node)
            # Strict < reserves 1 char for the comma appended after this check
            if len(line) < line_length:
                elements.append(line)
            else:
                lines = split_generic_value(node, indent, line_length)
                elements.append(" " * indent + lines[0])
                elements.extend(lines[1:])
    # Always add a trailing comma to the last non-comment element
    for i in range(len(elements) - 1, -1, -1):
        if elements[i].lstrip().startswith("#"):
            continue
        if not elements[i].endswith(","):
            elements[i] = elements[i] + ","
        break

    # Attach inline comments to the end of their value lines
    for i, comment in sameline_comments.items():
        elements[i] = elements[i] + SAMELINE_COMMENT_SEPARATOR + comment
    return elements


# ---------------------------------------------------------------------------
# Promise formatting
# ---------------------------------------------------------------------------


def _has_stakeholder(children: list[Node]) -> bool:
    """Check if promise children include a stakeholder node."""
    return any(c.type == "stakeholder" for c in children)


def _can_single_line_promise(node: Node, indent: int, line_length: int) -> bool:
    """Check if a promise can be formatted entirely on one line.

    Returns False for multi-attribute promises, promises with a
    half_promise continuation, or stakeholder+attribute combinations.
    """
    assert node.type == "promise"
    children = node.children
    if _contains_macro(children):
        return False
    if _contains_list_with_comment(children):
        return False
    if any(c.type == "comment" for c in children):
        # Comments between promiser and attribute force multi-line layout
        return False
    attrs = [c for c in children if c.type == "attribute"]
    next_sib = node.next_named_sibling
    while next_sib and next_sib.type == "macro":
        next_sib = next_sib.next_named_sibling

    if not attrs and not _has_stakeholder(children):
        # Promises without attributes and without stakeholder are
        # always single line
        return True
    if len(attrs) > 1:
        # We always want to multiline a promise with multiple attributes
        # even if it would fit on one line, i.e. this should be split:
        # "foo" string => "bar", comment => "baz";
        return False
    if next_sib and next_sib.type == "half_promise":
        # When the parser encounters promises which are split up
        # by macros, these are stored as "half" promises.
        # In such cases, we do not want to single line them.
        return False
    if _has_stakeholder(children) and attrs:
        # If a promise has both stakeholder and attribute(s) we want to
        # multiline it, i.e. this should be split:
        # "foo" -> { "bar" } string => "baz";
        return False
    if _has_stakeholder(children) and _stakeholder_needs_splitting(
        children, indent, line_length
    ):
        # A promise with a stakeholder and no attributes might be split
        # if the stakeholders are too long or include comments, i.e. split this:
        # "foo" -> { "bar", "too long", "stakeholders", "list", "in here", "now" };
        assert not attrs  # Should have already returned in if above
        return False

    # A candidate for single line - promise with either only stakeholder(s)
    # or only one attribute. All that's left is to construct the full line
    # to see how long it is. Examples:
    # "foo" -> { "this", "fits" };
    # "foo" string => "This also fits";
    line = _promiser_line_with_stakeholder(children)
    assert line
    if attrs:
        assert len(attrs) == 1
        line += " " + stringify_single_line_node(attrs[0]) + ";"
    else:
        line += ";"

    # This is kind of the "main" / obvious check - does it fit on one line:
    return indent + len(line) <= line_length


def _format_promise(
    node: Node,
    children: list[Node],
    fmt: Formatter,
    indent: int,
    line_length: int,
) -> bool:
    """Format a promise node. Returns True if handled, False to fall through."""
    # Single-line promise
    if _can_single_line_promise(node, indent, line_length):
        prefix = _promiser_line_with_stakeholder(children)
        assert prefix is not None
        attr = next((c for c in children if c.type == "attribute"), None)
        if attr:
            line = prefix + " " + stringify_single_line_node(attr) + ";"
        else:
            line = prefix + ";"
        fmt.print(line, indent)
        return True

    # Multi-line with split stakeholder
    if _has_stakeholder(children) and _stakeholder_needs_splitting(
        children, indent, line_length
    ):
        attrs = [c for c in children if c.type == "attribute"]
        promiser = _promiser_text(children)
        assert promiser is not None
        fmt.print(promiser + " -> {", indent)

        list_node = _get_stakeholder_list(children)
        assert list_node is not None
        middle = list_node.children[1:-1]
        element_indent = indent + 4
        elements = _format_stakeholder_elements(middle, element_indent, line_length)
        fmt.print_lines(elements, indent=0)

        close_indent = indent + 2
        if attrs:
            fmt.print("}", close_indent)
            _format_remaining_children(children, fmt, indent, line_length)
        else:
            fmt.print("};", close_indent)
        return True

    # Multi-line with inline stakeholder
    prefix = _promiser_line_with_stakeholder(children)
    if prefix:
        fmt.print(prefix, indent)
        _format_remaining_children(children, fmt, indent, line_length)
        return True

    return False


def _format_remaining_children(
    children: list[Node],
    fmt: Formatter,
    indent: int,
    line_length: int,
) -> None:
    """Format promise children, skipping promiser/arrow/stakeholder parts."""
    for child in children:
        if child.type in PROMISER_PARTS:
            continue
        _autoformat(child, fmt, line_length, indent)


# ---------------------------------------------------------------------------
# Block header formatting (bundle, body, promise blocks)
# ---------------------------------------------------------------------------


def _format_block_header(node: Node, fmt: Formatter) -> list[Node]:
    """Format a block header line and return the body's children for further processing."""
    header_parts: list[str] = []
    header_comments: list[str] = []
    # Direct comment children of the block (excludes comments inside a
    # parameter list) — candidates for being kept on the header line.
    direct_comments: list[Node] = []
    for x in node.children[0:-1]:
        if x.type == "comment":
            header_comments.append(text(x))
            direct_comments.append(x)
        elif x.type == "parameter_list":
            parts: list[str] = []
            for p in x.children:
                if p.type == "comment":
                    header_comments.append(text(p))
                else:
                    parts.append(text(p))
            header_parts[-1] = header_parts[-1] + stringify_parameter_list(parts)
        else:
            header_parts.append(text(x))
    line = " ".join(header_parts)
    # A lone same-line comment stays on the header line. When the header has
    # more than one comment, keep them together on their own lines below the
    # header instead of splitting one off onto the header line.
    if (
        len(header_comments) == 1
        and len(direct_comments) == 1
        and _is_sameline_comment(direct_comments[0])
    ):
        line += SAMELINE_COMMENT_SEPARATOR + header_comments[0]
        header_comments = []
    if not fmt.empty:
        prev_sib = node.prev_named_sibling
        # Skip over preceding empty comments since they will be removed
        while prev_sib and prev_sib.type == "comment" and _is_empty_comment(prev_sib):
            prev_sib = prev_sib.prev_named_sibling
        # Macros are always tightly surrounded — never a blank line after one
        follows_macro = prev_sib is not None and prev_sib.type == "macro"
        if not (prev_sib and prev_sib.type == "comment") and not follows_macro:
            fmt.blank_line()
    fmt.print(line, 0)
    for i, comment in enumerate(header_comments):
        if comment.strip() == "#":
            prev_is_comment = i > 0 and header_comments[i - 1].strip() != "#"
            next_is_comment = (
                i + 1 < len(header_comments) and header_comments[i + 1].strip() != "#"
            )
            if not (prev_is_comment and next_is_comment):
                continue
        fmt.print(comment, 0)
    return node.children[-1].children


# ---------------------------------------------------------------------------
# Blank line logic
# ---------------------------------------------------------------------------


def _needs_blank_line_before(child: Node, indent: int, line_length: int) -> bool:
    """Check if a blank separator line should precede this child node."""
    # Inline (trailing) comments are appended to the preceding line, so they
    # must never be preceded by a blank separator line.
    if child.type == "comment" and _is_sameline_comment(child):
        return False

    prev = child.prev_named_sibling
    # Empty comments preceding this child will be dropped — look past them
    # so we evaluate against the real prior content.
    while prev and prev.type == "comment" and _is_empty_comment(prev):
        prev = prev.prev_named_sibling
    if not prev:
        return False

    # Macros are always tightly surrounded — never a blank line after one
    if prev.type == "macro":
        return False

    if child.type == "bundle_section":
        return prev.type == "bundle_section"

    if child.type == "promise":
        if prev.type in {"promise", "half_promise"}:
            promise_indent = indent + 2
            both_single = (
                prev.type == "promise"
                and _can_single_line_promise(prev, promise_indent, line_length)
                and _can_single_line_promise(child, promise_indent, line_length)
            )
            return not both_single

    if child.type == "class_guarded_promise_block_attributes":
        return prev.type in {"attribute", "class_guarded_promise_block_attributes"}

    if child.type == "class_guarded_body_attributes":
        return prev.type in {"attribute", "class_guarded_body_attributes"}

    if child.type in CLASS_GUARD_TYPES:
        return prev.type in {"promise", "half_promise", "class_guarded_promises"}

    if child.type == "comment":
        if _is_empty_comment(child):
            return False
        # Top-level comment after a complete block — visually separates them
        if prev.type in BLOCK_TYPES:
            return True
        parent = child.parent
        # Comment at bundle_block_body level after a bundle_section —
        # either a trailing comment for the previous section or a leading
        # comment for the next one. Either way, visually separate it.
        if (
            prev.type == "bundle_section"
            and parent
            and parent.type == "bundle_block_body"
        ):
            return True
        if parent and parent.type in {"bundle_section", "class_guarded_promises"}:
            return prev.type in {"promise", "half_promise"} | CLASS_GUARD_TYPES
        if parent and parent.type in {
            "body_block_body",
            "promise_block_body",
            "class_guarded_body_attributes",
        }:
            # A comment is separated by a blank line from a preceding
            # attribute or a preceding class-guarded block, so it reads as
            # a leading comment for what follows (or a trailing comment for
            # the block). This mirrors how a comment after a promise is
            # separated in bundles.
            return prev.type == "attribute" or prev.type in CLASS_GUARD_TYPES
        return False

    return False


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------


def _is_sameline_comment(node: Node) -> bool:
    """Check if a comment sits on the same source line as the element before it.

    A trailing comment shares the row of the preceding sibling (which may be
    a leaf like ',' or ';', or a whole promise/attribute). Such comments are
    kept on the same output line as that element rather than moved to their
    own line."""
    if node.type != "comment":
        return False
    prev = node.prev_sibling
    if prev is None:
        return False
    return prev.end_point[0] == node.start_point[0]


def _is_empty_comment(node: Node) -> bool:
    """Check if a bare '#' comment should be dropped.

    A run of bare '#' between two non-empty comments collapses to a
    single '#' — the first in the run is kept, the rest are dropped.
    Bare '#' not flanked by real comments is always dropped."""
    if text(node).strip() != "#":
        return False
    prev = node.prev_named_sibling
    # Not the first in a run of '#' — a kept '#' precedes us, drop.
    if prev and prev.type == "comment" and text(prev).strip() == "#":
        return True
    # First in a run — kept only if a non-empty comment sits on each side
    # (forward search walks past the rest of the run to find one).
    if not (prev and prev.type == "comment"):
        return True
    nxt = node.next_named_sibling
    while nxt and nxt.type == "comment" and text(nxt).strip() == "#":
        nxt = nxt.next_named_sibling
    if not (nxt and nxt.type == "comment"):
        return True
    return False


def _skip_comments(sibling: Node | None, direction: str = "next") -> Node | None:
    """Walk past adjacent comment and macro siblings to find the nearest content node."""
    while sibling and sibling.type in ("comment", "macro"):
        sibling = (
            sibling.next_named_sibling
            if direction == "next"
            else sibling.prev_named_sibling
        )
    return sibling


def _comment_indent(node: Node, indent: int) -> int:
    """Compute indentation for a leaf comment based on its nearest non-comment neighbor."""
    nearest = _skip_comments(node.next_named_sibling, "next")
    # A trailing comment with no next sibling aligns with the deepest
    # content at the end of the previous sibling subtree, so that comments
    # appended after a class-guarded block visually belong to that block.
    if nearest is None:
        # A comment between a promiser and its terminating ';' is a promise
        # continuation — indent it past the promiser.
        if node.parent and node.parent.type in {"promise", "half_promise"}:
            return indent + 2
        prev = _skip_comments(node.prev_named_sibling, "prev")
        if prev and prev.type in INDENTED_TYPES:
            return _trailing_comment_indent(prev, indent)
        nearest = prev
    if nearest and nearest.type in INDENTED_TYPES:
        return indent + 2
    # No indented sibling found — if we're directly inside a block body,
    # indent so the comment lines up with where promises/attributes would.
    if nearest is None and node.parent and node.parent.type in BLOCK_BODY_TYPES:
        return indent + 2
    return indent


def _trailing_comment_indent(prev: Node, indent: int) -> int:
    """Walk down the end of a sibling subtree to compute the deepest indent."""
    while prev.type in INDENTED_TYPES:
        indent += 2
        deeper = None
        for child in reversed(prev.children):
            if child.type in INDENTED_TYPES:
                deeper = child
                break
        if deeper is None:
            break
        prev = deeper
    return indent


# ---------------------------------------------------------------------------
# Main recursive formatter
# ---------------------------------------------------------------------------


def _autoformat(
    node: Node,
    fmt: Formatter,
    line_length: int,
    indent: int = 0,
) -> None:
    """Recursively format a tree-sitter node tree into the Formatter buffer."""
    previous = fmt.update_previous(node)

    # Macro handling
    if previous and previous.type == "macro" and text(previous).startswith("@else"):
        if node.type != "half_promise":
            indent = fmt.macro_indent
    if node.type == "macro":
        if text(node).startswith("@if"):
            fmt.macro_indent = indent
        fmt.print(node, 0)
        return

    # Block header (bundle/body/promise blocks)
    children = node.children
    if node.type in BLOCK_TYPES:
        children = _format_block_header(node, fmt)

    # Indentation
    if node.type in INDENTED_TYPES:
        indent += 2

    # Attribute — stringify and return
    if node.type == "attribute":
        fmt.print_lines(_stringify(node, indent, line_length), indent=0)
        return

    # Promise — delegate to promise formatter
    if node.type == "promise":
        if _format_promise(node, children, fmt, indent, line_length):
            return

    # Interior node with children — recurse
    if children:
        for child in children:
            if _needs_blank_line_before(child, indent, line_length):
                fmt.blank_line()
            _autoformat(child, fmt, line_length, indent)
        return

    # Leaf nodes
    if node.type in {",", ";"}:
        if previous and previous.type == "macro":
            fmt.print(node, indent + 2)
        elif previous and previous.type == "comment":
            # A '#' comment runs to end of line, so a following ';'/',' can't
            # share that line — put it on its own continuation line.
            fmt.print(node, indent + 2)
        else:
            fmt.print_same_line(node)
    elif node.type == "comment":
        if _is_empty_comment(node):
            pass
        elif _is_sameline_comment(node) and not fmt.empty:
            fmt.print_same_line(SAMELINE_COMMENT_SEPARATOR + text(node))
        else:
            fmt.print(node, _comment_indent(node, indent))
    else:
        fmt.print(node, indent)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def format_policy_file(filename: str, line_length: int, check: bool) -> FormatResult:
    """Format a .cf policy file in place, writing only if content changed.

    Raises PolicySyntaxError when the file has syntax errors."""
    assert filename.endswith(".cf")

    PY_LANGUAGE = Language(tscfengine.language())
    parser = Parser(PY_LANGUAGE)

    with open(filename, "rb") as f:
        original_data = f.read()
    tree = parser.parse(original_data)

    root_node = tree.root_node
    assert root_node.type == "source_file"

    check_policy_syntax(tree, filename)

    fmt = Formatter()
    _autoformat(root_node, fmt, line_length)

    new_data = fmt.buffer
    if not new_data.endswith("\n"):
        # TODO: Look into why formatter sometimes outputs
        #       trailing newline and other times not.
        new_data += "\n"
    assert new_data.endswith("\n") and not new_data.endswith("\n\n")

    if new_data != original_data.decode("utf-8"):
        if check:
            print(f"Policy file '{filename}' needs reformatting")
            return FormatResult.NEEDS_FORMAT

        with open(filename, "w") as f:
            f.write(new_data)
        print(f"Policy file '{filename}' was reformatted")
        return FormatResult.REFORMATTED
    return FormatResult.NO_CHANGE


def format_policy_fin_fout(
    fin: IO[str], fout: IO[str], line_length: int, check: bool
) -> FormatResult:
    """Format CFEngine policy read from fin, writing the result to fout.

    Raises PolicySyntaxError when the input has syntax errors."""
    PY_LANGUAGE = Language(tscfengine.language())
    parser = Parser(PY_LANGUAGE)

    original_data = fin.read().encode("utf-8")
    tree = parser.parse(original_data)

    root_node = tree.root_node
    assert root_node.type == "source_file"

    check_policy_syntax(tree, "<stdin>")

    fmt = Formatter()
    _autoformat(root_node, fmt, line_length)

    new_data = fmt.buffer + "\n"
    fout.write(new_data)
    if new_data != original_data.decode("utf-8"):
        return FormatResult.REFORMATTED
    return FormatResult.NO_CHANGE


def _format_filename(filename: str, line_length: int, check: bool) -> FormatResult:
    """Format a single file.

    Raises PolicySyntaxError for .cf files with syntax errors."""
    if filename.endswith(".json"):
        return format_json_file(filename, check)
    if filename.endswith(".cf"):
        return format_policy_file(filename, line_length, check)
    raise UserError(f"Unrecognized file format: {filename}")


def _combine_results(a: FormatResult, b: FormatResult) -> FormatResult:
    """Combine two format results. A non-NO_CHANGE result takes precedence.

    Within a single format run, `check` is fixed, so NEEDS_FORMAT and
    REFORMATTED never occur together — one always dominates NO_CHANGE."""
    return b if b != FormatResult.NO_CHANGE else a


def _format_dirname(directory: str, line_length: int, check: bool) -> FormatResult:
    """Format files in directory recursively."""
    ret = FormatResult.NO_CHANGE
    for root, dirs, files in os.walk(directory):
        # Don't recurse into hidden folders
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            if name.startswith("."):
                continue  # Hidden files are ignored by default
            if (
                name.endswith(".x.cf")
                or name.endswith(".input.cf")
                or name.endswith(".output.cf")
                or name.endswith(".expected.cf")
            ):
                continue  # Test files skipped during directory traversal
            if name.endswith(
                (".input.json", ".jqinput.json", ".x.json", ".expected.json")
            ):
                continue  # Test files skipped during directory traversal
            filepath = os.path.join(root, name)
            if name.endswith(".json") or name.endswith(".cf"):
                ret = _combine_results(
                    ret, _format_filename(filepath, line_length, check)
                )
    return ret


def _format_paths_inner(names, line_length, check) -> FormatResult:
    if not names:
        return _format_dirname(".", line_length, check)
    if len(names) == 1 and names[0] == "-":
        # Special case, format policy file from stdin to stdout
        return format_policy_fin_fout(sys.stdin, sys.stdout, line_length, check)

    ret = FormatResult.NO_CHANGE
    for name in names:
        if name == "-":
            raise UserError(
                "The - argument has a special meaning and cannot be combined with other paths"
            )
        if not os.path.exists(name):
            raise UserError(f"{name} does not exist")
        if os.path.isfile(name):
            ret = _combine_results(ret, _format_filename(name, line_length, check))
            continue
        if os.path.isdir(name):
            ret = _combine_results(ret, _format_dirname(name, line_length, check))
            continue
    return ret


def format_paths(names, line_length, check) -> int:
    """Format the given paths (files and/or directories), or stdin.

    With no names, formats the current directory. A single "-" formats
    policy from stdin to stdout. Returns 0 on success (or no reformat
    needed), 1 when reformatting is needed in check mode or a policy file
    has syntax errors."""
    try:
        r = _format_paths_inner(names, line_length, check)
        if r == FormatResult.NEEDS_FORMAT:
            assert check
            # File needs reformatting
            return 1
        assert r in (FormatResult.NO_CHANGE, FormatResult.REFORMATTED)
        # Success - no format needed or successfully reformatted
        return 0
    except PolicySyntaxError as e:
        print(f"Error: {e}")
        return 1
