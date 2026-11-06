"""
Linting of CFEngine related files.

Currently implemented for:
- *.cf (policy files)
- cfbs.json (CFEngine Build project files)
- *.json (basic JSON syntax checking)
- *.csv (basic CSV syntax + RFC 4180 CRLF record terminator check)
- *.yml / *.yaml (basic YAML syntax checking)

This is performed in 3 steps:
1. Parsing - Read the .cf files and convert them into syntax trees
2. Discovery - Walk the syntax trees and record what is defined (bundles,
   bodies, promise types)
3. Linting - Walk the syntax trees again and use linting rules to check for
   errors

By default, linting is strict about bundles and bodies being defined
somewhere in the supplied files / folders. This can be disabled using the
`--strict=no` flag.

Usage:
$ cfengine lint
$ cfengine lint ./core/ ./masterfiles/
$ cfengine lint --strict=no main.cf

Todos:
- state.start_file(), state.end_file(), and state.get_pretty_filename()
  are a bit awkward. Could make iteration nicer.
"""

from copy import deepcopy
from enum import Enum
import os
import json
from typing import Callable, Iterable
import tree_sitter_cfengine as tscfengine
from dataclasses import dataclass, field
from tree_sitter import Language, Node, Parser, Tree
from cfbs.validate import validate_config
from cfbs.cfbs_config import CFBSConfig
from cfengine_cli.lint_csv import check_csv_file
from cfengine_cli.lint_yml import check_yml_file
from cfengine_cli.utils import UserError

LINT_EXTENSIONS = (".cf", ".cf.sub", ".json", ".csv", ".yml", ".yaml")
DEFAULT_NAMESPACE = "default"
VARS_TYPES = {
    "data",
    "ilist",
    "int",
    "real",
    "rlist",
    "slist",
    "string",
    "str",  # deprecated shorthand for string
}
PROMISE_BLOCK_ATTRIBUTES = ("path", "interpreter")

IMPLIES_BUNDLE = {"usebundle", "servicebundle", "service_bundle"}
IMPLIES_BODY = {"copy_from", "action"}
# Generally, IMPLIES_BUNDLE and IMPLIES_BODY might not be necessary
# in the future, when we're using syntax-description.json we will
# know if we expect a bundle or body (based on both promise type and attribute name)
# so "guessing" based on only attribute name can be dropped.

KNOWN_FAULTY_FUNCTION_DEFS = {"regex_replace", "peers"}
# Generally, we don't want to allow creating bodies / bundles with the same
# name as a built-in function, as it can make things more confusing
# (harder to read / understand policy and look up definitions).
#
# There are a couple of pre-existing cases that we need to preserve for backwards
# compatibility - bodies / bundles defined in masterfiles which have the same
# name as a built-in function.


@dataclass
class SyntaxData:
    BUILTIN_BODY_TYPES = {}
    BUILTIN_BUNDLE_TYPES = {}
    BUILTIN_PROMISE_TYPES = {}
    BUILTIN_FUNCTIONS = {}

    def __init__(self, syntax_path=None):
        """Load the bundled syntax-description.json and derive lookup dicts."""
        self._data_dict = self._load_syntax_description(path=syntax_path)
        self._derive_syntax_dicts(self._data_dict)

        assert self.BUILTIN_BODY_TYPES
        assert self.BUILTIN_BUNDLE_TYPES
        assert self.BUILTIN_PROMISE_TYPES
        assert self.BUILTIN_FUNCTIONS

    def _load_syntax_description(self, path: str | None = None) -> dict:
        """Load and return the parsed syntax-description.json file."""
        if path is None:
            path = os.path.join(os.path.dirname(__file__), "syntax-description.json")
        with open(path, "r") as f:
            return json.load(f)

    def _derive_syntax_dicts(self, data: dict):
        """Derive the five dictionaries used for linting from a loaded syntax-description json-file.
        sets the (BUILTIN_BODY_TYPES, BUILTIN_BUNDLE_TYPES, BUILTIN_PROMISE_TYPES, BUILTIN_FUNCTIONS, DEPRECATED_PROMISE_TYPES) dicts
        """
        builtin_body_types = data.get("bodyTypes", {})

        builtin_bundle_types = data.get("bundleTypes", {})

        builtin_promise_types = data.get("promiseTypes", {})

        builtin_functions = data.get("functions", {})

        deprecated_promise_types = {
            promise: builtin_promise_types.get(promise, {})
            for promise in {
                "defaults",
                "guest_environments",
            }  # Has to be hardcoded, not tagged in syntax-description.json
        }

        self.BUILTIN_BODY_TYPES = builtin_body_types
        self.BUILTIN_BUNDLE_TYPES = builtin_bundle_types
        self.BUILTIN_PROMISE_TYPES = builtin_promise_types
        self.BUILTIN_FUNCTIONS = builtin_functions
        self.DEPRECATED_PROMISE_TYPES = deprecated_promise_types


def _qualify(name: str, namespace: str) -> str:
    """If name is already qualified (contains ':'), return as-is. Otherwise prepend namespace."""
    assert '"' not in namespace
    assert '"' not in name
    if ":" in name:
        return name
    return f"{namespace}:{name}"


class Mode(Enum):
    """We go through these states in order, and have asserts to check that
    expected state is set."""

    NONE = None
    SYNTAX = 1  # Parse policy file and check for errors or empty
    DISCOVER = 2  # Find user defined bodies / bundles / promise types
    LINT = 3  # Run linting rules on syntax tree and check for errors


@dataclass
class Snippet:
    """A snippet is a policy file from a markdown code block.

    When we're linting these, we keep additional information about the original
    file and location."""

    original_filename: str
    original_line: int  # The beginning line number of the snippet
    i: int  # 1-indexed number of snippet within original file


class PolicyFile:
    """This class represents a parsed policy file

    The file is parsed once in the constructor, and then the syntax tree is
    reused when we iterate over it multiple times.

    We store filename, raw data (bytes), array of lines, and syntax tree/nodes.
    This is a bit of "duplication", but they are useful for printing nice
    linting errors.

    This is intended as a read-only view of the policy file, not to be used for
    formatting / changing the policy.

    Might be expanded in the future to include more information such as:
    - Whether the file is empty
    - Whether the file has syntax errors
    - Whether the file uses macros (Macros can indicate we need to be less strict)
    - Things defined and referenced in the file (bundles, bodies, promise types)
    """

    def __init__(self, filename: str, snippet: Snippet | None = None):
        """Parse the policy file at `filename` and flatten its syntax tree.

        `snippet` is set when the file is a temporary file extracted from a
        markdown code block, so error messages can refer back to the original.
        """
        self.filename = filename
        tree, lines, original_data = _parse_policy_file(filename)
        self.tree = tree
        self.lines = lines
        self.original_data = original_data
        self.snippet: Snippet | None = snippet

        # Flatten tree so it is easier to iterate over:
        self.nodes: list[Node] = []

        def visit(x):
            self.nodes.append(x)
            return 0

        _walk_callback(tree.root_node, visit)

    def __deepcopy__(self, _):
        """Overrides deepcopy for state-snapshotting.
        treesitter-tree is not pickleable, and PolicyFile
        should not change across state snapshots
        """
        return self


@dataclass
class State:
    """This class is used to keep track of needed state while linting.

    Not used in parsing, that is handled entirely by the tree sitter library.
    Used when we are iterating over (walking) nodes in the syntax tree.

    It has 3 different sets of information:
    1. Where we are in policy (policy file, attribute name, promise type etc.)
    2. Things defined in the policy set (bundles, bodies, promise types)
    3. Information needed to print correct linting errors (prefix, snippet information)
    """

    block_keyword: str | None = None  # "bundle" | "body" | "promise" | None
    block_type: str | None = None
    block_name: str | None = None
    promise_type: str | None = None  # "vars" | "files" | "classes" | ... | None
    attribute_name: str | None = None  # "if" | "string" | "slist" | ... | None
    namespace: str = DEFAULT_NAMESPACE  # "ns" | "default" | ... |
    macro: str | None = None  # "minimum_version()", "else", "between_versions()"
    old_state: dict = field(default_factory=dict)
    mode: Mode = Mode.NONE
    walking: bool = False
    strict: bool = True
    inside_call: bool = False  # True when nested inside another call's arguments
    call_depth: int = 0  # tracks call nesting; inside_call is call_depth > 1
    bundles = {}
    bodies = {}
    custom_promise_types = {}
    policy_file: PolicyFile | None = None
    prefix: str | None = None

    def print_summary(self) -> None:
        """Useful to print relevant information when debugging."""
        print("Bundles")
        print(self.bundles)
        print("Bodies")
        print(self.bodies)
        print("Custom promise types")
        print(self.custom_promise_types)

    def get_pretty_filename(self) -> str:
        """Filename, or code block number and location"""
        assert self.policy_file
        snippet = self.policy_file.snippet
        if not snippet:
            return self.policy_file.filename
        return f"Code block {snippet.i} (cf3) at '{snippet.original_filename}:{snippet.original_line}'"

    def get_location(self, line: int, column: int) -> str:
        """File location (including line and col) used in error messages and
        clickable in the right terminals / IDEs."""
        assert self.policy_file
        filename = self.policy_file.filename
        snippet = self.policy_file.snippet
        if snippet:
            # If we are in a snippet (code block in markdown file), translate
            # to original filename and correct line number in that file:
            filename = snippet.original_filename
            line = snippet.original_line + line
        return f"{filename}:{line}:{column}"

    def get_location_extended(self, line: int, column: int) -> str:
        """String to put in error messages which specifies where the issue is
        and whether it is in a code block."""
        location = self.get_location(line, column)
        assert self.policy_file
        if not self.policy_file.snippet:
            return f"at {location}"
        return f"in code block at {location}"

    def block_string(self) -> str | None:
        """Returns for example "body file control" when you are inside body
        file control block."""
        if not (self.block_keyword and self.block_type and self.block_name):
            return None

        return " ".join((self.block_keyword, self.block_type, self.block_name))

    def start_file(self, policy_file: PolicyFile):
        """Should be called before the first state.navigate() when iterating
        over a policy file syntax tree."""
        assert policy_file
        assert not self.walking
        assert self.mode != Mode.NONE

        self.policy_file = policy_file
        self.namespace = DEFAULT_NAMESPACE
        self.walking = True

    def end_file(self) -> None:
        """Should be called after the last state.navigate() when iterating
        over a policy file syntax tree.

        Note: state.snippet is NOT automatically cleared. Caller needs to make
              sure state.snippet is set and cleared appropriately when working
              with code blocks. This is because the normal case is to iterate
              over the same snippet (file) 2-3 times, so it would be annoying
              if you have to reset snippet after each .end_file().
        """
        assert self.walking
        assert self.mode != Mode.NONE

        # These should normally be unset automatically (for a valid policy
        # file). However, if we are "aborting" in the middle of a policy
        # file with syntax errors, we need to clear them.
        self.block_keyword = None
        self.block_type = None
        self.block_name = None
        self.promise_type = None
        self.attribute_name = None

        self.walking = False
        self.policy_file = None

    def _add_definition(self, name: str, node: Node, definitions: dict) -> None:
        """Add a definition (bundle or body) to the given dictionary.

        The value for each qualified name is a list of definitions, since
        the same name can appear multiple times (e.g. inside macro if/else
        branches). Each definition records the file, line, and parameter
        list.
        """
        name = _qualify(name, self.namespace)
        assert self.policy_file
        n = node.next_named_sibling
        if n and n.type == "parameter_list":
            _, *args, _ = n.children
            parameters = list(filter(",".__ne__, iter(_text(x) for x in args)))
        else:
            parameters = []

        definition = {
            "filename": self.policy_file.filename,
            "line": node.range.start_point[0] + 1,
            "column": node.range.start_point[1] + 1,
            "parameters": parameters,
        }
        if self.macro:
            definition["macro"] = self.macro
        if name not in definitions:
            definitions[name] = []
        definitions[name].append(definition)

    def add_bundle(self, name: str, node: Node) -> None:
        """This is called during discovery wherever a bundle is defined."""
        self._add_definition(name, node, self.bundles)

    def add_body(self, name: str, node: Node) -> None:
        """This is called during discovery wherever a body is defined.

        Control bodies are a special case and should not be passed here.
        """
        self._add_definition(name, node, self.bodies)

    def add_promise_type(self, name: str) -> None:
        """This is called during discovery wherever a custom promise type is
        defined.

        For example:
        promise agent example
        {
          interpreter => "/usr/bin/python3";
          path => "/var/cfengine/inputs/modules/promises/git.py";
        }
        """
        self.custom_promise_types[name] = True

    def navigate(self, node) -> None:
        """This function is called whenever we move to a node, to update the
        state accordingly.

        For example:
        - When we encounter a closing } for a bundle, we want to set
          block_keyword from "bundle" to None
        """
        assert self.mode != Mode.NONE
        assert self.walking

        # Some sanity checks - we want to see that state is correctly updated:
        if node.type in ("}", ";", "->", "=>"):
            # "Inside" a block
            assert self.block_keyword and self.block_type and self.block_name
            assert self.block_keyword in ("bundle", "body", "promise")
        if node.type in (";", "->", "=>") and self.block_keyword == "bundle":
            # "Inside" a non-empty bundle
            assert self.promise_type
        if node.type in ("->") and self.block_keyword == "bundle":
            # Stakeholder arrow means we must be in bundle
            assert self.block_keyword == "bundle"
        if node.type == "=>":
            assert self.attribute_name

        # Beginnings of blocks:
        if node.type in (
            "bundle_block_keyword",
            "body_block_keyword",
            "promise_block_keyword",
        ):
            self.block_keyword = _text(node)
            assert self.block_keyword in ("bundle", "body", "promise")
            return
        if node.type in (
            "bundle_block_type",
            "body_block_type",
            "promise_block_type",
        ):
            self.block_type = _text(node)
            assert self.block_type
            return
        if node.type in (
            "bundle_block_name",
            "body_block_name",
            "promise_block_name",
        ):
            self.block_name = _text(node)
            assert self.block_name
            return

        # Update namespace inside body file control:
        if (
            self.block_string() == "body file control"
            and self.attribute_name == "namespace"
            and node.type == "quoted_string"
        ):
            self.namespace = _text(node)[1:-1]
            return

        # New promise type (bundle section) inside a bundle:
        if node.type == "promise_guard":
            self.promise_type = _text(node)[:-1]  # strip trailing ':'
            return

        if node.type == "attribute_name":
            self.attribute_name = _text(node)
            return

        # Attributes always end with ; in all 3 block types
        if node.type == ";":
            self.attribute_name = None
            self.call_depth = 0
            self.inside_call = False
            return

        # Track call nesting so we can recognize nested calls as built-in
        # functions even when the attribute name implies bundle/body.
        if node.type == "call":
            self.call_depth += 1
            self.inside_call = self.call_depth > 1
            return
        if node.type == ")" and node.parent and node.parent.type == "call":
            self.call_depth -= 1
            self.inside_call = self.call_depth > 1
            return

        # Clear things when ending a top level block:
        if node.type == "}" and node.parent.type != "list":
            assert self.attribute_name is None  # Should already be cleared by ;
            assert node.parent
            assert node.parent.type in [
                "bundle_block_body",
                "promise_block_body",
                "body_block_body",
            ]
            self.block_keyword = None
            self.block_type = None
            self.block_name = None
            self.promise_type = None
            return

        if node.type == "macro":
            macro_type = _text(node)
            if macro_type.startswith("@if"):
                self.old_state = deepcopy(self.__dict__)
                self.macro = macro_type.split(" ")[-1]
            elif macro_type.startswith("@else"):
                self.macro = "else"
                self.old_state = deepcopy(self.__dict__)
                self.__dict__.update(self.old_state)
            elif macro_type.startswith("@endif"):
                self.macro = None
                self.__dict__.update(self.old_state)
                # NOTE: this or that? maybe a dict of states based on the macro-type?
                self.old_state = {}
            return


def _last_real_sibling_before(node: Node) -> Node | None:
    """Walk backwards from node through its preceding siblings, skipping
    comments and macros, to find the last real (content) sibling."""
    sib = node.prev_sibling
    while sib is not None and sib.type in ("comment", "macro"):
        sib = sib.prev_sibling
    return sib


def _check_syntax(policy_file: PolicyFile, state: State) -> int:
    """Iterate over a syntax tree and print errors if it is empty, has syntax
    errors, or is missing expected tokens (e.g. a dropped ';').

    Notably, printing syntax errors _does not happen during parsing_.

    Tree sitter has already fully parsed the policy, we iterate over the result
    and see if it has inserted any "ERROR" nodes, or any "MISSING" nodes
    (tree-sitter's error recovery can silently insert a synthetic token,
    e.g. a missing ';' right before a closing '}', without ever producing
    an ERROR node.

    Stops at first error. Returns number of errors, always 0 or 1 in this case.
    """
    assert state.mode == Mode.SYNTAX
    filename = policy_file.filename
    lines = policy_file.lines
    if not policy_file.tree.root_node.children:
        snippet = policy_file.snippet
        if snippet:
            print(
                f"Error: Empty policy snippet {snippet.i} at '{snippet.original_filename}:{snippet.original_line}'"
            )
        else:
            print(f"Error: Empty policy file '{filename}'")
        return 1

    assert policy_file.tree.root_node.type == "source_file"

    state.start_file(policy_file)
    for node in policy_file.nodes:
        state.navigate(node)
        line = node.range.start_point[0] + 1
        column = node.range.start_point[1] + 1
        if node.type == "ERROR":
            _highlight_range(node, lines)
            location = state.get_location_extended(line, column)
            print(f"Error: Syntax error {location}")
            state.end_file()
            return 1
        if node.is_missing:
            # To avoid pointing at #comment/@macro -lines
            prev = _last_real_sibling_before(node)
            report_node = prev if prev is not None else node
            _highlight_range(report_node, lines)
            line = report_node.range.end_point[0] + 1
            column = report_node.range.end_point[1] + 1
            missing = node.type if not node.is_named else f"token: {node.type}"
            location = state.get_location_extended(line, column)
            print(f"Error: Missing '{missing}' {location}")
            state.end_file()
            return 1
    state.end_file()
    return 0


def _discover_node(node: Node, state: State) -> int:
    """Look at a single node and if it's the name of a bundle / body / promise
    block, add it to state.

    control bodies are skipped.

    Returns number of errors, always 0 in this case."""

    # Define bodies:
    if node.type == "body_block_name":
        name = _text(node)
        if name == "control":
            return 0  # No need to define control blocks
        state.add_body(name, node)
        return 0

    # Define bundles:
    if node.type == "bundle_block_name":
        name = _text(node)
        state.add_bundle(name, node)
        return 0

    # Define custom promise types:
    if node.type == "promise_block_name":
        name = _text(node)
        state.add_promise_type(name)
        return 0

    return 0


def _discover(policy_file: PolicyFile, state: State) -> int:
    """Discover all user defined bodies / bundles / promise types in a policy
    file and adds them to state.

    Returns number of errors, always 0 in this case.
    """
    assert state.mode == Mode.DISCOVER
    state.start_file(policy_file)
    for node in policy_file.nodes:
        state.navigate(node)
        _discover_node(node, state)
    state.end_file()
    return 0


def _lint_promise_guard(
    node: Node, state: State, location: str, syntax_data: SyntaxData
):
    """Check a promise type guard (e.g. `vars:`) for deprecation or unknown type."""
    assert _text(node) and len(_text(node)) > 1 and _text(node)[-1] == ":"
    promise_type = _text(node)[0:-1]
    if state.strict and promise_type in syntax_data.DEPRECATED_PROMISE_TYPES:
        raise ValidationError(
            f"Deprecation: Promise type '{promise_type}' is deprecated {location}",
            node,
        )
    if (
        state.strict
        and promise_type not in syntax_data.BUILTIN_PROMISE_TYPES
        and promise_type not in state.custom_promise_types
    ):
        raise ValidationError(
            f"Error: Undefined promise type '{promise_type}' {location}", node
        )


def _lint_block_type(node: Node, state: State, location: str, syntax_data: SyntaxData):
    """Check that a block type (e.g. `agent` in `bundle agent main`) is valid."""
    if (
        node.type == "bundle_block_type"
        and _text(node) not in syntax_data.BUILTIN_BUNDLE_TYPES
    ):
        raise ValidationError(
            f"Error: Bundle type must be one of ({', '.join(syntax_data.BUILTIN_BUNDLE_TYPES)}), not '{_text(node)}' {location}",
            node,
        )
    if node.type == "promise_block_type" and _text(node) != "agent":
        raise ValidationError(
            f"Error: Promise type must be 'agent', not '{_text(node)}' {location}",
            node,
        )


def _lint_block_name(node: Node, state: State, location: str, syntax_data: SyntaxData):
    """Check that a block name follows conventions and doesn't shadow a built-in."""
    assert node.type in ("bundle_block_name", "body_block_name", "promise_block_name")

    if (
        state.strict
        and node.type == "bundle_block_name"
        and _text(node) != _text(node).lower()
    ):
        raise ValidationError(
            f"Convention: Bundle name should be lowercase {location}", node
        )
    if (
        state.strict
        and node.type == "body_block_name"
        and _text(node) != _text(node).lower()
    ):
        raise ValidationError(
            f"Convention: Body name should be lowercase {location}", node
        )
    if node.type == "promise_block_name" and _text(node) != _text(node).lower():
        raise ValidationError(
            f"Convention: Promise type should be lowercase {location}", node
        )
    if state.strict and (
        node.type in ("bundle_block_name", "body_block_name")
        and _text(node) in syntax_data.BUILTIN_FUNCTIONS
        and _text(node) not in KNOWN_FAULTY_FUNCTION_DEFS
    ):
        raise ValidationError(
            f"Error: {'Bundle' if 'bundle' in node.type else 'Body'} '{_text(node)}' conflicts with built-in function with the same name {location}",
            node,
        )


def _lint_promise(node: Node, state: State, location: str, _syntax_data: SyntaxData):
    """Check that a vars-promise has exactly one value-typed attribute (string, slist, ...)."""
    assert node.type == "promise"
    if state.promise_type == "vars":
        attribute_nodes = [x for x in node.children if x.type == "attribute"]
        # Attributes are children of a promise, and attribute names are children of attributes
        # Need to iterate inside to find the attribute name (data, ilist, int, etc.)
        value_nodes = []
        for attr in attribute_nodes:
            for child in attr.children:
                if child.type != "attribute_name":
                    continue
                if _text(child) in VARS_TYPES:
                    # Ignore the other attributes which are not values
                    value_nodes.append(child)

        if not value_nodes:
            # None of vars_types were found
            raise ValidationError(
                f"Error: Missing value for vars promise {_text(node)[:-1]} {location}",
                node,
            )

        if len(value_nodes) > 1:
            # Too many of vars_types were found
            # TODO: We could improve _highlight_range to highlight multiple nodes in a nice way
            nodes = ", ".join([_text(x) for x in value_nodes])
            raise ValidationError(
                f"Error: Mutually exclusive attribute values ({nodes})"
                f" for a single promiser inside vars-promise {location}",
                value_nodes[-1],
            )


def _lint_calling_identifier(
    node: Node, state: State, location: str, syntax_data: SyntaxData
):
    """Check that a function/bundle/body call name resolves correctly.

    Behavior depends on context: nested calls must be built-in functions,
    `IMPLIES_BUNDLE` / `IMPLIES_BODY` attributes restrict the kind of
    definition allowed, and a few rules apply only inside vars-promises and
    custom promise types.
    """
    assert node.type == "calling_identifier"
    name = _text(node)
    if name.startswith("$"):
        # Variable expansion so no point in checking if it's defined
        return
    qualified_name = _qualify(name, state.namespace)
    is_bundle = qualified_name in state.bundles
    is_body = qualified_name in state.bodies
    is_function = name in syntax_data.BUILTIN_FUNCTIONS

    if state.inside_call:
        # Nested calls must be built-in functions - the surrounding
        # attribute's IMPLIES_BUNDLE/IMPLIES_BODY only applies to the
        # outermost call.
        if not is_function:
            if is_bundle:
                error = f"Error: Expected a built-in function but '{name}' is a bundle {location}"
            elif is_body:
                error = f"Error: Expected a built-in function but '{name}' is a body {location}"
            else:
                error = f"Error: Call to unknown function '{name}' {location}"
            raise ValidationError(error, node)
        return
    if state.strict and is_bundle and state.promise_type in state.custom_promise_types:
        raise ValidationError(
            f"Error: Call to bundle '{name}' inside custom promise: '{state.promise_type}' {location}",
            node,
        )
    if state.strict:
        implies_bundle = state.attribute_name in IMPLIES_BUNDLE
        implies_body = state.attribute_name in IMPLIES_BODY

        error = None
        if implies_bundle and not is_bundle:
            if is_body:
                error = f"Error: Expected a bundle but '{name}' is a body {location}"
            elif is_function:
                error = f"Error: Expected a bundle but '{name}' is a built-in function {location}"
            else:
                error = f"Error: Call to unknown bundle '{name}' {location}"
        elif implies_body and not is_body:
            if is_bundle:
                error = f"Error: Expected a body but '{name}' is a bundle {location}"
            elif is_function:
                error = f"Error: Expected a body but '{name}' is a built-in function {location}"
            else:
                error = f"Error: Call to unknown body '{name}' {location}"
        elif (
            not implies_bundle
            and not implies_body
            and not is_bundle
            and not is_body
            and not is_function
        ):
            error = (
                f"Error: Call to unknown function / bundle / body '{name}' {location}"
            )

        if error:
            raise ValidationError(error, node)
    if (
        not is_function
        and state.promise_type == "vars"
        and state.attribute_name not in ("action", "classes")
    ):
        raise ValidationError(
            f"Error: Call to unknown function '{name}' inside 'vars'-promise {location}",
            node,
        )
    if (
        state.promise_type == "vars"
        and state.attribute_name in ("action", "classes")
        and not is_body
    ):
        raise ValidationError(
            f"Error: '{name}' is not a defined body. Only bodies may be called with '{state.attribute_name}' {location}",
            node,
        )


def _lint_attribute_name(
    node: Node, state: State, location: str, syntax_data: SyntaxData
):
    """Check an attribute name for deprecations and validity according to the
    surrounding promise type."""
    assert node.type == "attribute_name"
    attribute_name = _text(node)
    assert attribute_name == state.attribute_name
    if state.strict and state.promise_type == "vars" and attribute_name == "str":
        raise ValidationError(
            f"Deprecation: Use 'string' instead of 'str' {location}", node
        )
    if state.strict and attribute_name == "ifvarclass":
        raise ValidationError(
            f"Deprecation: Use 'if' instead of 'ifvarclass' {location}", node
        )
    if state.promise_type and attribute_name:
        promise_type_data = syntax_data.BUILTIN_PROMISE_TYPES.get(
            state.promise_type, {}
        )
        if not promise_type_data:
            # Custom promise type - we cannot validate attribute name here.
            return
        promise_type_attrs = promise_type_data.get("attributes", {})
        if attribute_name not in promise_type_attrs:
            raise ValidationError(
                f"Error: Invalid attribute '{attribute_name}' for promise type '{state.promise_type}' {location}",
                node,
            )
    if state.block_keyword == "promise" and attribute_name not in (
        None,
        *PROMISE_BLOCK_ATTRIBUTES,
    ):
        raise ValidationError(
            f"Error: Invalid attribute name '{attribute_name}' in '{state.block_name}' custom promise type definition {location}",
            node,
        )


def _lint_call(node: Node, state: State, location: str, syntax_data: SyntaxData):
    """Check a call's argument count against its built-in / bundle / body signature."""
    call, _, *args, _ = node.children  # f ( a1 , a2 , a..N )
    call = _text(call)
    if call in KNOWN_FAULTY_FUNCTION_DEFS:
        return

    args = list(filter(",".__ne__, iter(_text(x) for x in args if x.type != "comment")))

    if call in syntax_data.BUILTIN_FUNCTIONS and (
        state.inside_call
        or (
            state.attribute_name not in IMPLIES_BUNDLE
            and state.attribute_name not in IMPLIES_BODY
        )
    ):
        func = syntax_data.BUILTIN_FUNCTIONS.get(call, {})
        variadic = func.get("variadic", True)
        # variadic meaning variable amount of arguments allowed
        # -1, -1 // default -- all required, aka. non-variadic func
        # 1, -1  // 1-n
        # 0, -1  // 0-n
        # 2, 3   // 2-3
        min_args = func.get("minArgs", -1)
        max_args = func.get("maxArgs", -1)
        if variadic:
            assert min_args != -1
            assert min_args != max_args
            if max_args == -1:
                max_args = float("inf")  # N args allowed
        else:
            assert min_args == -1 and max_args == -1
            # If min args -1 (meaning all required), max should be the same
            # All args required, use len of parameter list
            min_args = max_args = len(func.get("parameters", []))

        if not (min_args <= len(args) <= max_args):
            argc_str = (
                f"at least {min_args}"
                if max_args == float("inf")
                else (
                    f"{min_args}-{max_args}" if min_args != max_args else str(max_args)
                )
            )
            raise ValidationError(
                f"Error: Expected {argc_str} arguments, received {len(args)} for function '{call}' {location}",
                node,
            )

    qualified_name = _qualify(call, state.namespace)
    if (
        state.strict
        and not state.inside_call
        and qualified_name in state.bundles
        and state.attribute_name not in IMPLIES_BODY
    ):
        definitions = state.bundles[qualified_name]
        valid_counts = {len(d.get("parameters", [])) for d in definitions}
        if len(args) not in valid_counts:
            counts = sorted(valid_counts)
            expected = " or ".join(str(c) for c in counts)
            raise ValidationError(
                f"Error: Expected {expected} arguments, received {len(args)} for bundle '{call}' {location}",
                node,
                [_definition_hint("bundle", call, definitions)],
            )
    if (
        state.strict
        and not state.inside_call
        and qualified_name in state.bodies
        and state.attribute_name not in IMPLIES_BUNDLE
    ):
        definitions = state.bodies[qualified_name]
        valid_counts = {len(d.get("parameters", [])) for d in definitions}
        if len(args) not in valid_counts:
            counts = sorted(valid_counts)
            expected = " or ".join(str(c) for c in counts)
            raise ValidationError(
                f"Error: Expected {expected} arguments, received {len(args)} for body '{call}' {location}",
                node,
                [_definition_hint("body", call, definitions)],
            )


def _lint_half_promise(node: Node, state: State, location: str):
    """Check if a half-promise (a promise split by a macro) is well-formed.

    Half-promises are only valid as macro branches, and only one half-promise
    is allowed per macro branch.
    """
    assert node.type == "half_promise"

    prev_sib = node.prev_named_sibling
    while prev_sib and prev_sib.type == "comment":
        prev_sib = prev_sib.prev_named_sibling
    prev_type = prev_sib.type if prev_sib else None
    if not state.macro:
        raise ValidationError(
            f"Error: Found promise attribute with no parent-promiser outside of a macro {location}",
            node,
        )
    elif prev_type != "macro":
        raise ValidationError(
            f"Error: Multiple promise attributes with ending semicolon found inside macro '{state.macro}' {location}",
            node,
        )


def _lint_node(
    node: Node, policy_file: PolicyFile, state: State, syntax_data: SyntaxData
) -> None:
    """Checks we run on each node in the syntax tree,
    utilizes state for checks which require context.

    Raises ValidationError when a check fails. The exception carries the node
    to highlight, the error message, and any hint lines; the caller renders
    them.
    """

    line = node.range.start_point[0] + 1
    column = node.range.start_point[1] + 1
    location = state.get_location_extended(line, column)

    if node.type in ("bundle_block_type", "body_block_type", "promise_block_type"):
        # The type is what comes after body / bundle / promise keyword
        # and before the name. Ex: common, agent, copy_from, classes
        _lint_block_type(node, state, location, syntax_data)
    if node.type in ("bundle_block_name", "body_block_name", "promise_block_name"):
        # The name of the bundle, body, or promise type (where it's defined).
        _lint_block_name(node, state, location, syntax_data)
    if node.type == "promise_guard":
        # The promise guard is the beginning of a section inside a bundle,
        # or, in other words, the promise type + one colon
        _lint_promise_guard(node, state, location, syntax_data)
    if node.type == "promise":
        # The promise node has the promiser, stakeholder, and all the attributes
        # inside (as children).
        _lint_promise(node, state, location, syntax_data)
    if node.type == "half_promise":
        # A half promise is an artifact of how a macro can split up a promise
        # into 2 branching parts (3 parts in total).
        _lint_half_promise(node, state, location)
    if node.type == "attribute_name":
        # Attribute name nodes refer to all the cases; inside a body, inside
        # a promise block and inside a bundle (inside a promise).
        _lint_attribute_name(node, state, location, syntax_data)
    if node.type == "call":
        # A call is a bare name (not inside quotes) plus parentheses,
        # optionally with arguments inside, or even nested function calls.
        _lint_call(node, state, location, syntax_data)
    if node.type == "calling_identifier":
        # A calling identifier is the name of the function / body / bundle
        # that is being called, the part before the parentheses
        _lint_calling_identifier(node, state, location, syntax_data)


def _pass_fail_filename(filename: str, errors: int) -> str:
    """String to print whether a file passed or failed, includes number of
    errors in case of failure."""
    if errors == 0:
        return f"PASS: {filename}"
    error_string = f"{errors} error{'s' if errors > 1 else ''}"
    return f"FAIL: {filename} ({error_string})"


def _pass_fail_state(state: State, errors: int) -> str:
    """String to print whether a file passed or failed.

    Uses state to get appropriate information for code blocks if necessary.
    Must be called before state.end_file().
    """
    pretty_filename = state.get_pretty_filename()
    return _pass_fail_filename(pretty_filename, errors)


def _lint(policy_file: PolicyFile, state: State, syntax_data: SyntaxData) -> int:
    """Run linting rules (checks) on nodes in a policy file syntax tree."""
    assert state.mode == Mode.LINT
    errors = 0
    state.start_file(policy_file)
    for node in policy_file.nodes:
        state.navigate(node)
        try:
            _lint_node(node, policy_file, state, syntax_data)
        except ValidationError as e:
            _highlight_range(e.node, policy_file.lines)
            print(e.message)
            for hint in e.hints:
                print(hint)
            errors += 1
    message = _pass_fail_state(state, errors)
    state.end_file()
    if state.prefix:
        print(state.prefix, end="")
    print(message)
    return errors


def _lint_check_args(args: list[str]):
    """Validate user-supplied paths exist, are file/folder, and have a supported extension.

    Raises UserError on invalid input.
    """
    for i, arg in enumerate(args):
        if not os.path.exists(arg):
            raise UserError(f"'{arg}' does not exist")
        if not os.path.isfile(arg) and not os.path.isdir(arg):
            raise UserError(f"'{arg}' must be a file or folder")
        if os.path.isfile(arg) and not arg.endswith(LINT_EXTENSIONS):
            raise UserError(
                f"'{arg}' has an unsupported file extension, must be one of: {', '.join(LINT_EXTENSIONS)}"
            )
        if arg in args[i + 1 :]:
            raise UserError(f"Duplicate argument '{arg}'")


def _find_filenames_in_arg_folder(arg: str) -> list[str]:
    """Find filenames with correct extensions recursively within a folder.

    Skip hidden files. Don't recurse into hidden folders or folders named out."""
    assert os.path.isdir(arg)
    results = []
    for root, dirs, files in os.walk(arg, followlinks=True):
        # Remove hidden files:
        files = [f for f in files if not f[0] == "."]
        # Skip .x.cf files (policy files with intentional errors)
        # and .output.cf files (formatter test outputs):
        files = [f for f in files if not f.endswith((".x.cf", ".output.cf"))]
        # Skip test-related JSON files during directory traversal:
        files = [
            f
            for f in files
            if not f.endswith(
                (".input.json", ".jqinput.json", ".x.json", ".expected.json")
            )
        ]
        for name in files:
            if name.endswith(LINT_EXTENSIONS):
                results.append(os.path.join(root, name))

        for d in dirs:
            assert not d.startswith("./") and not d.endswith("/")

        # Modify dirs used by os.walk in next iteration;
        # Remove hidden folders and out folders
        dirs[:] = [d for d in dirs if not d[0] == "." and not d == "out"]
    return sorted(results)


def _args_to_filenames(args: list[str]) -> list[str]:
    """Convert a list of arguments into a list of filenames.

    Preserves the order.

    For folders, it looks for files inside recursively with the right extension.

    Inside the specified folders hidden files beginning with . are ignored.
    Inside the specified folders cfbs-generated /out/ folders are also ignored.
    """
    results = []
    for arg in args:
        assert os.path.exists(arg)
        if os.path.isfile(arg):
            assert arg.endswith(LINT_EXTENSIONS)
            results.append(arg)
            continue
        assert os.path.isdir(arg)
        from_folder = _find_filenames_in_arg_folder(arg)
        if not from_folder:
            raise UserError(f"No files to lint found in '{arg}'")
        results.extend(from_folder)
    # User may have specified a folder and a file inside it
    # This causes duplicate entries, let's accept that and remove them:
    unique = list(dict.fromkeys(results))
    return unique


def _lint_main(
    args: list[str],
    strict: bool,
    state=None,
    snippet: Snippet | None = None,
    syntax_data=None,
    syntax_path: str | None = None,
) -> int:
    """This is the main function used for linting, it does all the steps on all
    the arguments (files / folders).

    Summarized, it does:
    1. Find all filenames.
    2. Syntax check / lint JSON files.
    3. Parse policy files into syntax trees and check for syntax errors.
    4. Discover user defined bundles / bodies / promise types in syntax trees.
    5. Lint policy files (syntax trees) printing errors based on checks.

    If there are syntax errors, it stops early, the last 2 steps are not performed.

    Returns number of errors."""

    # Check that user supplied args exist, are files / folders, and have
    # correct extensions:
    _lint_check_args(args)

    errors = 0

    if state is None:
        state = State()
    state.strict = strict
    state.mode = Mode.SYNTAX

    if syntax_data is None:
        try:
            syntax_data = SyntaxData(syntax_path)
        except FileNotFoundError:
            raise UserError(f"'{syntax_path}' does not exist")

    filenames = _args_to_filenames(args)

    if snippet:
        assert len(args) == 1
        assert len(filenames) == 1
        assert os.path.isfile(args[0])
        assert args[0].endswith(".cf")

    policy_files = []
    for filename in filenames:
        assert os.path.isfile(filename)
        if filename.endswith(".json"):
            errors += _lint_json_selector(filename)
            continue
        if filename.endswith(".csv"):
            errors += _lint_csv(filename)
            continue
        if filename.endswith((".yml", ".yaml")):
            errors += _lint_yml(filename)
            continue
        assert filename.endswith((".cf", ".cf.sub"))
        policy_file = PolicyFile(filename, snippet)
        r = _check_syntax(policy_file, state)
        errors += r
        if r != 0:
            state.start_file(policy_file)
            print(_pass_fail_state(state, r))
            state.end_file()
            continue
        policy_files.append(policy_file)
    if errors != 0:
        return errors
    state.mode = Mode.DISCOVER

    for policy_file in policy_files:
        errors += _discover(policy_file, state)

    state.mode = Mode.LINT

    for policy_file in policy_files:
        errors += _lint(policy_file, state, syntax_data)

    return errors


def _highlight_range(node: Node, lines: list[str]) -> None:
    """Highlight which line and which part of the line is problematic."""
    line = node.range.start_point[0] + 1
    column = node.range.start_point[1]

    length = len(lines[line - 1]) - column
    if node.range.start_point[0] == node.range.end_point[0]:
        # Starts and ends on same line:
        length = node.range.end_point[1] - node.range.start_point[1]
    assert length >= 1

    print("")

    # Print previous line if any, for context
    if line >= 2:
        print(lines[line - 2])

    # Print the problematic line:
    print(lines[line - 1])

    # Print the arrows on the next line pointing at the problematic part:
    marker = "^"
    if length > 2:
        marker += "-" * (length - 2)
    if length > 1:
        marker += "^"
    print(" " * column + marker)


def _text(node: Node) -> str:
    """Get the string / text of a syntax node, i.e. what was actually written
    in the policy file."""
    assert node.text
    return node.text.decode()


def _definition_hint(kind: str, name: str, definitions: list[dict]) -> str:
    """Build a single 'Hint:' line, joining all definition locations with ' and '."""
    locations = " and ".join(
        f"{d['filename']}:{d['line']}:{d['column']}" for d in definitions
    )
    return f"Hint: The {kind} '{name}' is defined at {locations}"


def _walk_callback(node: Node, callback: Callable[[Node], int]) -> int:
    """Recursively walk a syntax tree, calling the callback on each node."""
    assert node
    assert callback

    errors = 0
    errors += callback(node)
    for child in node.children:
        _walk_callback(child, callback)
    return errors


def _parse_policy_file(filename: str) -> tuple[Tree, list[str], bytes]:
    """Parse a policy file into a syntax tree using tree sitter.

    This function is used by PolicyFile constructor, in most cases it is better
    to call PolicyFile(filename) instead of this function."""
    assert os.path.isfile(filename)
    PY_LANGUAGE = Language(tscfengine.language())
    parser = Parser(PY_LANGUAGE)

    with open(filename, "rb") as f:
        original_data = f.read()
    tree = parser.parse(original_data)
    lines = original_data.decode().split("\n")

    return tree, lines, original_data


def _lint_cfbs_json(filename: str) -> int:
    """Parse a cfbs.json file, using the code from cfbs."""
    assert os.path.isfile(filename)
    assert filename.endswith("cfbs.json")

    config = CFBSConfig.get_instance(filename=filename, non_interactive=True)
    r = validate_config(config)

    print(_pass_fail_filename(filename, r))
    return r


def _lint_json_plain(filename: str) -> int:
    """Lint a JSON file, essentially just checking that it parses
    successfully."""
    assert os.path.isfile(filename)

    with open(filename, "r") as f:
        data = f.read()
    r = 0
    try:
        json.loads(data)
    except json.JSONDecodeError:
        r = 1

    print(_pass_fail_filename(filename, r))
    return r


def _lint_json_selector(file: str) -> int:
    """Lint a JSON file using the cfbs function for cfbs.json and the generic
    option otherwise."""
    assert os.path.isfile(file)
    if file.endswith("/cfbs.json"):
        return _lint_cfbs_json(file)
    assert file.endswith(".json")
    return _lint_json_plain(file)


def _lint_csv(filename: str) -> int:
    """Lint a CSV file: check that csv parses, and that record terminators
    are CRLF (per RFC 4180)."""
    assert os.path.isfile(filename)
    problem = check_csv_file(filename)
    r = 0
    if problem is not None:
        print(f"{filename}: {problem}")
        r = 1
    print(_pass_fail_filename(filename, r))
    return r


def _lint_yml(filename: str) -> int:
    """Lint a YAML file: check that it parses and contains at least one
    non-null document."""
    assert os.path.isfile(filename)
    problem = check_yml_file(filename)
    r = 0
    if problem is not None:
        print(f"{filename}: {problem}")
        r = 1
    print(_pass_fail_filename(filename, r))
    return r


# ---------------------------------------------------------------------------
# Syntax error detection (used by both linter and formatter)
# ---------------------------------------------------------------------------


def _find_first_error(node: Node) -> Node | None:
    """Find the first ERROR node in the tree, or None if the tree is valid."""
    if node.type == "ERROR":
        return node
    for child in node.children:
        found = _find_first_error(child)
        if found:
            return found
    return None


class PolicySyntaxError(Exception):
    """Raised when a policy file has syntax errors and cannot be formatted."""

    def __init__(self, filename: str, line: int, column: int):
        """Record the location of the syntax error and format a default message."""
        self.filename = filename
        self.line = line
        self.column = column
        super().__init__(f"Syntax error in '{filename}' at {filename}:{line}:{column}")


class ValidationError(Exception):
    """Raised by _lint_node when a linting check fails.

    Carries the node to highlight, the error message, and any hint lines so
    the caller can render them.
    """

    def __init__(self, message: str, node: Node, hints: list[str] | None = None):
        """Store the error message, the node to highlight, and any hint lines."""
        self.message = message
        self.node = node
        self.hints = hints or []
        super().__init__(message)


def check_policy_syntax(tree: Tree, filename: str) -> None:
    """Check a parsed tree for syntax errors.

    Raises PolicySyntaxError if an ERROR node is found.

    Only checks for ERROR nodes, not MISSING nodes — missing tokens like
    semicolons are handled gracefully by the formatter."""
    root_node = tree.root_node
    error_node = _find_first_error(root_node)
    if not error_node:
        return
    line = error_node.range.start_point[0] + 1
    column = error_node.range.start_point[1] + 1
    raise PolicySyntaxError(filename, line, column)


# Interface: These are the functions we want to be called from outside
# They create State() and should not be called recursively inside lint.py


def lint_single_file(file: str, strict: bool = True) -> int:
    """Lint a single file"""
    return _lint_main([file], strict)


def lint_args(
    args: Iterable[str], strict: bool = True, syntax_path: str | None = None
) -> int:
    """Lint a list of args (files / folders)"""
    return _lint_main(list(args), strict, syntax_path=syntax_path)


def lint_policy_file_snippet(
    filename: str,
    original_filename: str,
    original_line: int,
    snippet: int,
    prefix: str,
    strict: bool = True,
):
    """Lint a policy file snippet (extracted from a markdown code block)."""
    assert prefix
    assert original_filename and os.path.isfile(original_filename)
    assert original_line and original_line > 0
    assert snippet and snippet > 0
    assert os.path.isfile(filename)
    assert filename.endswith((".cf", ".cfengine3", ".cf3", ".cf.sub"))

    state = State()
    state.strict = strict
    state.prefix = prefix
    return _lint_main(
        [filename],
        strict,
        state,
        snippet=Snippet(original_filename, original_line, snippet),
    )


def lint_json(filename: str) -> int:
    """Lint a single JSON file (cfbs.json gets cfbs validation, others basic parse)."""
    return _lint_json_selector(filename)
