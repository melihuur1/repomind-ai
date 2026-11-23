"""Finds the one declaration a constant's name means on given lines of a file. Tree-sitter, no compiler.

The question is always about a use: this name, on these lines. The same name can mean different
things in two functions of one file, or in two files of one repository, so a declaration found
anywhere else is not an answer. Whatever cannot be decided from the syntax is refused, because a
wrong declaration shown beside real code looks like evidence.

TypeScript and JavaScript follow lexical scope. A name means the nearest binding around its use:
a ``const`` or ``let`` in an enclosing block, a ``var`` in an enclosing function, or a name the
file imports. A parameter, a destructured name, a loop or catch variable, a function, a class or
an enum of that name hides everything outside it and is never followed. An import is followed
only into a relative module, to a declaration at the top level of that file. ``Class.NAME`` is
followed to a static field with a value, when ``Class`` means a class the same file declares.
Any other ``object.NAME``, and ``this.NAME``, is not followed.

Java follows the order the language itself uses. A plain ``NAME`` means a local variable declared
earlier in an enclosing block, then a field declared directly in an enclosing type, then a single
static import. ``Type.NAME`` means a field declared directly in ``Type``, and ``Type`` is looked
for in the types around the use, the top-level types of the file, a single-type import, the file's
own package, and last the on-demand imports. A single-type import of a type that is not in the
repository ends the search, so a library's ``Limits`` is never taken for the repository's own.

What is not handled, and so refused or not seen:

* a field inherited from a superclass or an interface. A type that extends or implements anything
  ends the search for a plain name, because the name could come from there;
* a nested type inherited from a supertype, which Java would prefer to an imported type of the
  same name. Rare, and not detected;
* ``package.Type.NAME``, ``variable.NAME``, static on-demand imports, re-exports, default and
  namespace imports, and anything a build tool or a path alias decides.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from repomind.parsing.languages import parser_for
from repomind.store.database import ChunkRow

SCRIPT_LANGUAGES = frozenset({"typescript", "tsx", "javascript"})
LANGUAGES = SCRIPT_LANGUAGES | {"java"}
_MODULE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js")
_NAME_NODES = frozenset({"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"})


class Sources(Protocol):
    """What may be read, all at the commit the passages come from."""

    def text(self, path: str) -> str | None: ...

    def language(self, path: str) -> str | None: ...

    def chunks(self, path: str) -> Sequence[ChunkRow]: ...

    def type_paths(self, name: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class Declaration:
    name: str
    path: str
    start_line: int
    end_line: int


Found = tuple[Declaration | None, str]
_NOTHING: Found = (None, "")


@dataclass(frozen=True)
class _Parsed:
    language: str
    data: bytes
    tree: object  # Kept so that its nodes stay valid.
    root: object


def _text(node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace") if node is not None else ""


def _same(first, second) -> bool:
    return first is not None and second is not None and first.id == second.id


def _field(node) -> str | None:
    """The name of the field this node fills in its parent, such as ``name`` or ``value``."""
    parent = node.parent
    if parent is None:
        return None
    for index in range(parent.child_count):
        if parent.child(index).id == node.id:
            return parent.field_name_for_child(index)
    return None


def _lines(node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _enclosing(node, kinds):
    node = node.parent
    while node is not None and node.type not in kinds:
        node = node.parent
    return node


def _root(node):
    while node.parent is not None:
        node = node.parent
    return node


# ------------------------------------------------------------ TypeScript and JavaScript

_PATTERNS = frozenset({"object_pattern", "array_pattern", "pair_pattern", "rest_pattern", "assignment_pattern", "object_assignment_pattern"})
# What a `const` or `let` is visible in, and what a `var` is.
_BLOCKS = frozenset({"program", "statement_block", "switch_body", "for_statement", "for_in_statement", "catch_clause", "class_static_block"})
_VAR_SCOPES = frozenset({"program", "function_declaration", "generator_function_declaration", "function_expression", "generator_function",
                         "arrow_function", "method_definition", "class_static_block", "internal_module", "module"})
_CLASSES = frozenset({"class_declaration", "abstract_class_declaration"})
_NAMED_IN_A_BLOCK = frozenset({"function_declaration", "generator_function_declaration", "function_signature", "enum_declaration", "internal_module", "module"})
_NAMED_INSIDE_ITSELF = frozenset({"function_expression", "generator_function", "class"})
_USE, _MEMBER = "use", "member"


@dataclass(frozen=True)
class _Binding:
    """Something that gives the name a meaning inside ``scope``.

    ``variable`` is a declaration with a value, which is what is looked for. ``import`` is a name
    from another module. ``class`` is a class this file declares, whose static fields can be read
    as ``Class.NAME``. ``other`` is anything else. The last two hide the name and are never followed.
    """

    kind: str
    scope: object
    statement: object = None
    module: str = ""
    original: str = ""


def _is_static_field(member, name_node=None) -> bool:
    """A class field written `static NAME = value`, which is what `Class.NAME` reads."""
    if member.type not in ("public_field_definition", "field_definition") or member.child_by_field_name("value") is None:
        return False
    named = member.child_by_field_name("name") or member.child_by_field_name("property")
    return (name_node is None or _same(named, name_node)) and any(child.type == "static" for child in member.children)


def _script_role(node, data: bytes):
    """What this occurrence of the name is: a binding, a use, a member of something, or None for neither."""
    parent = node.parent
    if parent is None:
        return None
    kind = node.type
    if kind == "property_identifier":
        # `object.NAME` reads a property. A key in an object literal or a method's own name reads nothing.
        if parent.type == "member_expression" and _same(parent.child_by_field_name("property"), node):
            return _MEMBER
        # A static field's own name means that field. Its scope is the name alone: nothing reads it without `Class.` before it.
        return _Binding("variable", node, parent) if _is_static_field(parent, node) else None
    if kind == "type_identifier":
        if parent.type in _CLASSES and _same(parent.child_by_field_name("name"), node):
            return _Binding("class", _enclosing(parent, _BLOCKS), parent)
        return None
    if kind == "shorthand_property_identifier":
        return _USE
    if kind not in ("identifier", "shorthand_property_identifier_pattern"):
        return None

    top = node
    while top.parent is not None and top.parent.type in _PATTERNS:
        holder = top.parent
        if holder.type in ("assignment_pattern", "object_assignment_pattern") and _same(holder.child_by_field_name("right"), top):
            return _USE  # A default value is read, not bound.
        top = holder
    owner = top.parent
    if owner is None:
        return None
    destructured = top.id != node.id or kind == "shorthand_property_identifier_pattern"

    if owner.type == "variable_declarator" and _same(owner.child_by_field_name("name"), top):
        statement = owner.parent
        scope = _enclosing(statement, _VAR_SCOPES if statement.type == "variable_declaration" else _BLOCKS)
        outside = statement.parent.type if statement.parent is not None else ""
        has_a_value = owner.child_by_field_name("value") is not None
        if destructured or not has_a_value or outside in ("ambient_declaration", "for_statement", "for_in_statement"):
            return _Binding("other", scope)
        return _Binding("variable", scope, statement)
    if owner.type in ("required_parameter", "optional_parameter"):
        if not _same(owner.child_by_field_name("pattern"), top):
            return _USE  # Its default value.
        holder = owner.parent
        return _Binding("other", holder.parent if holder is not None and holder.type == "formal_parameters" else holder)
    if owner.type == "formal_parameters":
        return _Binding("other", owner.parent)
    if owner.type == "arrow_function" and _same(owner.child_by_field_name("parameter"), top):
        return _Binding("other", owner)
    if owner.type == "catch_clause" and _same(owner.child_by_field_name("parameter"), top):
        return _Binding("other", owner)
    if owner.type == "for_in_statement" and _same(owner.child_by_field_name("left"), top):
        return _Binding("other", owner)
    if destructured:
        return _USE  # `({ NAME } = value)` assigns to a name bound somewhere else.

    if owner.type in _CLASSES and _same(owner.child_by_field_name("name"), node):
        return _Binding("class", _enclosing(owner, _BLOCKS), owner)
    if owner.type in _NAMED_IN_A_BLOCK and _same(owner.child_by_field_name("name"), node):
        return _Binding("other", _enclosing(owner, _BLOCKS))
    if owner.type in _NAMED_INSIDE_ITSELF and _same(owner.child_by_field_name("name"), node):
        return _Binding("other", owner)
    if owner.type == "import_specifier":
        alias = owner.child_by_field_name("alias")
        if alias is not None and not _same(alias, node):
            return None  # The name it has in the other module, which this file does not use.
        statement = _enclosing(owner, {"import_statement"})
        source = statement.child_by_field_name("source") if statement is not None else None
        return _Binding("import", _root(node), module=_text(source, data).strip("'\"`"), original=_text(owner.child_by_field_name("name"), data))
    if owner.type in ("import_clause", "namespace_import", "import_require_clause"):
        return _Binding("other", _root(node))
    if owner.type == "export_specifier":
        statement = _enclosing(owner, {"export_statement"})
        passed_on = statement is not None and statement.child_by_field_name("source") is not None
        return None if passed_on or _same(owner.child_by_field_name("alias"), node) else _USE
    return _USE


def _relative_module(path: str, module: str) -> list[str]:
    if not module.startswith("."):
        return []
    base = posixpath.normpath(posixpath.join(posixpath.dirname(path), module))
    bases = [base]
    for written in (".js", ".jsx"):  # TypeScript may import a module under the name its output will have.
        if base.endswith(written):
            bases.append(base[: -len(written)])
    return list(dict.fromkeys(candidate for item in bases for candidate in (item, *(item + suffix for suffix in _MODULE_SUFFIXES))))


def _top_level_declarations(parsed: _Parsed, name: str) -> list:
    """Statements at the top level of a module that give ``name`` a value. Nothing inside a function or a block."""
    found = []
    for child in parsed.root.named_children:
        statement = child.child_by_field_name("declaration") if child.type == "export_statement" else child
        if statement is None or statement.type not in ("lexical_declaration", "variable_declaration"):
            continue
        for declarator in statement.named_children:
            if declarator.type != "variable_declarator":
                continue
            named = declarator.child_by_field_name("name")
            if named is not None and named.type == "identifier" and _text(named, parsed.data) == name and declarator.child_by_field_name("value") is not None:
                found.append(child)
    return found


# ------------------------------------------------------------------------------- Java

_JAVA_TYPES = frozenset({"class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "annotation_type_declaration"})
_JAVA_BODIES = frozenset({"class_body", "interface_body", "enum_body", "annotation_type_body"})
_JAVA_SUPERTYPES = frozenset({"superclass", "super_interfaces", "extends_interfaces"})
_JAVA_PATTERNS = frozenset({"pattern", "type_pattern", "record_pattern", "record_pattern_component"})
_JAVA_IMPORT = re.compile(r"import\s+(static\s+)?([\w.$]+?)(\s*\.\s*\*)?\s*;")


def _java_members(body) -> list:
    members = list(body.named_children)
    for member in body.named_children:
        if member.type == "enum_body_declarations":
            members.extend(member.named_children)
    return members


def _java_name(node, data: bytes) -> str:
    return _text(node.child_by_field_name("name"), data)


def _java_declares(statement, data: bytes, name: str) -> bool:
    return any(d.type == "variable_declarator" and _java_name(d, data) == name for d in statement.named_children)


def _java_fields(body, data: bytes, name: str) -> list:
    """Fields called ``name`` that this type declares itself. An enum value and an inherited field are neither."""
    return [m for m in _java_members(body) if m.type in ("field_declaration", "constant_declaration") and _java_declares(m, data, name)]


def _java_binds(node, data: bytes, name: str) -> bool:
    """Whether this node gives ``name`` to a parameter or a variable of a loop, a catch or a try."""

    def named(item) -> bool:
        if item is None:
            return False
        if item.type == "identifier":
            return _text(item, data) == name
        if item.type == "spread_parameter":
            return _java_declares(item, data, name)
        return _java_name(item, data) == name

    kind = node.type
    if kind in ("method_declaration", "constructor_declaration", "compact_constructor_declaration", "record_declaration", "lambda_expression"):
        parameters = node.child_by_field_name("parameters")
        return parameters is not None and (named(parameters) if parameters.type == "identifier" else any(named(p) for p in parameters.named_children))
    if kind == "catch_clause":
        return any(named(child) for child in node.named_children if child.type == "catch_formal_parameter")
    if kind == "enhanced_for_statement":
        return named(node.child_by_field_name("name"))
    if kind == "for_statement":
        return any(child.type == "local_variable_declaration" and _java_declares(child, data, name) for child in node.named_children)
    if kind == "try_with_resources_statement":
        resources = node.child_by_field_name("resources")
        return resources is not None and any(named(resource) for resource in resources.named_children)
    return False


def _java_in_pattern(node) -> bool:
    """Whether this is the variable of ``x instanceof Type name`` or of a ``case Type name`` pattern."""
    parent = node.parent
    if parent is not None and parent.type == "instanceof_expression" and _field(node) == "name":
        return True
    for _ in range(4):
        if parent is None:
            return False
        if parent.type in _JAVA_PATTERNS:
            return True
        parent = parent.parent
    return False


def _java_header(parsed: _Parsed) -> tuple[str, list[tuple[bool, str, bool]]]:
    """The file's package, and its imports as (static, name, on demand)."""
    package, imports = "", []
    for child in parsed.root.named_children:
        if child.type == "package_declaration":
            named = [item for item in child.named_children if item.type in ("scoped_identifier", "identifier")]
            package = _text(named[-1], parsed.data) if named else ""
        elif child.type == "import_declaration":
            match = _JAVA_IMPORT.match(" ".join(_text(child, parsed.data).split()))
            if match:
                imports.append((bool(match.group(1)), match.group(2), bool(match.group(3))))
    return package, imports


def _java_types(parsed: _Parsed) -> list[tuple[str, object]]:
    """Every type the file declares outside a method, with its full name: package, enclosing types and its own."""
    found: list[tuple[str, object]] = []

    def visit(members, prefix: str) -> None:
        for member in members:
            if member.type not in _JAVA_TYPES:
                continue
            name = _java_name(member, parsed.data)
            qualified = f"{prefix}.{name}" if prefix else name
            found.append((qualified, member))
            body = member.child_by_field_name("body")
            if body is not None:
                visit(_java_members(body), qualified)

    visit(parsed.root.named_children, _java_header(parsed)[0])
    return found


# --------------------------------------------------------------------------- resolving


class Resolver:
    """Resolves names for one commit. Each file is read and parsed once."""

    def __init__(self, sources: Sources) -> None:
        self.sources = sources
        self._files: dict[str, _Parsed | None] = {}
        self._occurrences: dict[tuple[str, str], list] = {}

    def resolve(self, name: str, path: str, start_line: int, end_line: int) -> Found:
        """The one declaration ``name`` means on these lines of ``path``, or why there is none."""
        found, reason = self._resolve(name, path, start_line, end_line)
        return found, reason or ("" if found else f"no declaration of {name} was found")

    def _resolve(self, name: str, path: str, start_line: int, end_line: int) -> Found:
        if self.sources.language(path) is None:
            return None, f"{path} is not indexed"
        parsed = self._parsed(path)
        if parsed is None:
            return _NOTHING
        one = self._java if parsed.language == "java" else self._script
        results = []
        for node in self._named(path, name):
            if start_line <= node.start_point[0] + 1 <= end_line:
                result = one(path, parsed, node, name)
                if result is not None:
                    results.append(result)
        if not results:
            return _NOTHING
        # Every use on these lines has to mean the same declaration. One that cannot be followed leaves the name unknown.
        if any(declaration is None for declaration, _ in results):
            return None, next((reason for declaration, reason in results if declaration is None and reason), "")
        distinct = {declaration for declaration, _ in results}
        if len(distinct) > 1:
            return None, f"{name} means {len(distinct)} different declarations in these lines"
        return results[0]

    def _parsed(self, path: str) -> _Parsed | None:
        if path not in self._files:
            language = self.sources.language(path)
            text = self.sources.text(path) if language in LANGUAGES else None
            if text is None:
                self._files[path] = None
            else:
                data = text.encode("utf-8")
                tree = parser_for(language).parse(data)
                self._files[path] = _Parsed(language, data, tree, tree.root_node)
        return self._files[path]

    def _named(self, path: str, name: str) -> list:
        """Every place the name is written in the file as a name. Text in a comment or a string is not one."""
        key = (path, name)
        if key not in self._occurrences:
            parsed = self._parsed(path)
            wanted = name.encode("utf-8")
            found = []
            stack = [parsed.root] if parsed is not None else []
            while stack:
                node = stack.pop()
                if node.child_count:
                    stack.extend(reversed(node.children))
                elif node.type in _NAME_NODES and parsed.data[node.start_byte : node.end_byte] == wanted:
                    found.append(node)
            self._occurrences[key] = found
        return self._occurrences[key]

    # ------------------------------------------------------ TypeScript and JavaScript

    def _script(self, path: str, parsed: _Parsed, node, name: str) -> Found | None:
        role = _script_role(node, parsed.data)
        if role is None:
            return None
        if role == _MEMBER:
            return self._script_static_field(path, parsed, node, name)
        if isinstance(role, _Binding):
            binding = role
        else:
            innermost = self._script_nearest(path, parsed, node, name)
            if not innermost:
                return _NOTHING
            if len(innermost) > 1:
                declared = all(b.kind == "variable" for b in innermost)
                return None, (f"{name} is declared {len(innermost)} times in {path}" if declared else "")
            binding = innermost[0]
        if binding.kind == "variable":
            statement = binding.statement
            outer = statement.parent if statement.parent is not None and statement.parent.type == "export_statement" else statement
            return Declaration(name, path, *_lines(outer)), ""
        if binding.kind == "import":
            return self._imported(path, name, binding)
        return _NOTHING

    def _script_nearest(self, path: str, parsed: _Parsed, use, name: str) -> list[_Binding]:
        """What gives ``name`` its meaning at ``use``: the bindings of the innermost scope around it that has any."""
        steps_out: dict[int, int] = {}
        around = use
        while around is not None:
            steps_out[around.id] = len(steps_out)
            around = around.parent
        bindings = [b for b in (_script_role(n, parsed.data) for n in self._named(path, name)) if isinstance(b, _Binding)]
        visible = [(steps_out[b.scope.id], b) for b in bindings if b.scope is not None and b.scope.id in steps_out]
        nearest = min((steps for steps, _ in visible), default=None)
        return [b for steps, b in visible if steps == nearest]

    def _script_static_field(self, path: str, parsed: _Parsed, node, name: str) -> Found:
        """``Class.NAME``, when ``Class`` means a class this file declares and that class gives a static ``NAME`` a value.

        ``this.NAME``, an imported class and a longer path are not followed.
        """
        target = node.parent.child_by_field_name("object")
        if target is None or target.type != "identifier":
            return _NOTHING
        nearest = self._script_nearest(path, parsed, target, _text(target, parsed.data))
        if len(nearest) != 1 or nearest[0].kind != "class":
            return _NOTHING
        body = nearest[0].statement.child_by_field_name("body")
        fields = [
            member
            for member in (body.named_children if body is not None else [])
            if _is_static_field(member) and _text(member.child_by_field_name("name") or member.child_by_field_name("property"), parsed.data) == name
        ]
        return (Declaration(name, path, *_lines(fields[0])), "") if len(fields) == 1 else _NOTHING

    def _imported(self, path: str, name: str, binding: _Binding) -> Found:
        outside = f"{name} is imported from {binding.module}, which is not a file of this repository"
        files = [candidate for candidate in _relative_module(path, binding.module) if self.sources.language(candidate) is not None]
        if not files:
            return None, outside
        if len(files) > 1:
            return None, f"{name} is imported from {binding.module}, which could be {len(files)} files"
        target = self._parsed(files[0])
        if target is None or target.language not in SCRIPT_LANGUAGES:
            return None, outside
        statements = _top_level_declarations(target, binding.original)
        if len(statements) == 1:
            return Declaration(binding.original, files[0], *_lines(statements[0])), ""
        if statements:
            return None, f"{binding.original} is declared {len(statements)} times in {files[0]}"
        return None, f"{name} is imported from {binding.module}, which does not declare it itself"

    # ----------------------------------------------------------------------------- Java

    def _java(self, path: str, parsed: _Parsed, node, name: str) -> Found | None:
        parent = node.parent
        if node.type != "identifier" or parent is None:
            return None
        field = _field(node)
        if parent.type == "field_access" and field == "field":
            target = parent.child_by_field_name("object")
            if target is not None and target.type == "this":
                return self._java_own_field(path, parsed, node, name)
            qualifier = _text(target, parsed.data) if target is not None and target.type == "identifier" else ""
            # `a.b.Type.NAME` and `variable.NAME` are not followed. A type is written with a capital.
            return self._java_qualified(path, parsed, node, qualifier, name) if qualifier[:1].isupper() else _NOTHING
        if field == "name":
            statement = parent.parent
            declares = parent.type == "variable_declarator" and statement is not None
            if declares and statement.type in ("field_declaration", "constant_declaration"):
                return Declaration(name, path, *_lines(statement)), ""
            if declares and statement.type == "local_variable_declaration" and statement.parent is not None and statement.parent.type != "for_statement":
                return Declaration(name, path, *_lines(statement)), ""
            return None  # A name being given to something, not used.
        if field in ("key", "scope") or (parent.type == "lambda_expression" and field == "parameters") or parent.type == "inferred_parameters":
            return None
        if parent.type == "scoped_identifier" or _java_in_pattern(node):
            return None
        return self._java_plain(path, parsed, node, name)

    def _java_plain(self, path: str, parsed: _Parsed, use, name: str) -> Found:
        """A name with nothing before it: a local variable, a field of a type around it, or a static import."""
        data = parsed.data
        node = use
        while node.parent is not None:
            parent = node.parent
            if parent.type in ("block", "constructor_body", "switch_block_statement_group"):
                for earlier in parent.named_children:
                    if earlier.end_byte > node.start_byte:
                        break
                    if earlier.type == "local_variable_declaration" and _java_declares(earlier, data, name):
                        return Declaration(name, path, *_lines(earlier)), ""
            elif _java_binds(parent, data, name):
                return _NOTHING
            elif parent.type in _JAVA_BODIES:
                # `node` is the member the use is in. A pattern variable anywhere in it may be in scope.
                if any(node.start_byte <= other.start_byte < node.end_byte and _java_in_pattern(other) for other in self._named(path, name)):
                    return _NOTHING
                found = self._java_field_in(path, data, parent, name)
                if found is not None:
                    return found
                owner = parent.parent
                is_a_value_of_it = any(m.type == "enum_constant" and _java_name(m, data) == name for m in parent.named_children)
                may_inherit_it = owner is None or owner.type not in _JAVA_TYPES or any(child.type in _JAVA_SUPERTYPES for child in owner.named_children)
                if is_a_value_of_it or may_inherit_it:
                    return _NOTHING
            node = parent
        for static, imported, on_demand in _java_header(parsed)[1]:
            if static and not on_demand and "." in imported and imported.rsplit(".", 1)[1] == name:
                return self._java_field_of_type(imported.rsplit(".", 1)[0], name)
        return _NOTHING

    def _java_own_field(self, path: str, parsed: _Parsed, use, name: str) -> Found:
        body = _enclosing(use, _JAVA_BODIES)
        return (self._java_field_in(path, parsed.data, body, name) if body is not None else None) or _NOTHING

    def _java_field_in(self, path: str, data: bytes, body, name: str) -> Found | None:
        """The field this type declares itself, a refusal when it declares two, or None when it declares none."""
        found = _java_fields(body, data, name)
        if len(found) == 1:
            return Declaration(name, path, *_lines(found[0])), ""
        return (None, f"{name} is declared {len(found)} times in {path}") if found else None

    def _java_qualified(self, path: str, parsed: _Parsed, use, qualifier: str, name: str) -> Found:
        """``Type.NAME``: a field ``Type`` declares itself, with ``Type`` found the way Java finds it."""
        data = parsed.data
        node = use
        while node.parent is not None:
            parent = node.parent
            if parent.type in _JAVA_BODIES:
                owner = parent.parent
                if owner is not None and owner.type in _JAVA_TYPES and _java_name(owner, data) == qualifier:
                    return self._java_field_in(path, data, parent, name) or _NOTHING
                nested = [m for m in _java_members(parent) if m.type in _JAVA_TYPES and _java_name(m, data) == qualifier]
                if nested:
                    return self._java_field_of_node(path, data, nested[0], name) if len(nested) == 1 else _NOTHING
            node = parent
        for top in parsed.root.named_children:
            if top.type in _JAVA_TYPES and _java_name(top, data) == qualifier:
                return self._java_field_of_node(path, data, top, name)

        package, imports = _java_header(parsed)
        single = list(dict.fromkeys(imported for static, imported, on_demand in imports if not static and not on_demand and imported.rsplit(".", 1)[-1] == qualifier))
        if single:
            # It hides the package and every on-demand import. If it is not in the repository, nothing else is tried.
            return self._java_field_of_type(single[0], name) if len(single) == 1 else _NOTHING
        in_package = self._java_named(f"{package}.{qualifier}" if package else qualifier)
        if in_package is None or in_package:
            return self._java_field_of_matches(in_package, f"{package}.{qualifier}" if package else qualifier, name)
        on_demand_matches: list = []
        for static, imported, on_demand in imports:
            if on_demand and not static:
                matches = self._java_named(f"{imported}.{qualifier}")
                if matches is None:
                    return _NOTHING
                on_demand_matches.extend(matches)
        return self._java_field_of_matches(on_demand_matches, qualifier, name)

    def _java_named(self, qualified: str) -> list[tuple[str, _Parsed, object]] | None:
        """Types of the repository with exactly this full name. None when a file that might hold one cannot be read."""
        matches = []
        for candidate in dict.fromkeys(self.sources.type_paths(qualified.rsplit(".", 1)[-1])):
            parsed = self._parsed(candidate)
            if parsed is None or parsed.language != "java":
                return None
            matches.extend((candidate, parsed, node) for name, node in _java_types(parsed) if name == qualified)
        return matches

    def _java_field_of_type(self, qualified: str, name: str) -> Found:
        return self._java_field_of_matches(self._java_named(qualified), qualified, name)

    def _java_field_of_matches(self, matches, called: str, name: str) -> Found:
        if not matches:
            return _NOTHING
        if len(matches) > 1:
            return None, f"{len(matches)} types are called {called}"
        path, parsed, node = matches[0]
        return self._java_field_of_node(path, parsed.data, node, name)

    def _java_field_of_node(self, path: str, data: bytes, node, name: str) -> Found:
        body = node.child_by_field_name("body")
        return (self._java_field_in(path, data, body, name) if body is not None else None) or _NOTHING


def resolve(sources: Sources, name: str, path: str, start_line: int, end_line: int) -> Found:
    """The one declaration ``name`` means on these lines of ``path``, or why there is none."""
    return Resolver(sources).resolve(name, path, start_line, end_line)
