"""Tree-sitter grammars plus the node types the chunker needs for each language."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    import_types: frozenset[str]
    container_kinds: dict[str, str]
    callable_kinds: dict[str, str]
    atomic_kinds: dict[str, str]
    wrapper_types: frozenset[str]
    variable_types: frozenset[str]
    function_value_types: frozenset[str]
    field_types: frozenset[str]
    comment_types: frozenset[str]
    decorator_types: frozenset[str]


_JAVA = LanguageSpec(
    name="java",
    import_types=frozenset({"package_declaration", "import_declaration"}),
    container_kinds={
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "annotation_type_declaration": "annotation",
    },
    callable_kinds={
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "compact_constructor_declaration": "constructor",
    },
    atomic_kinds={},
    wrapper_types=frozenset(),
    variable_types=frozenset(),
    function_value_types=frozenset(),
    field_types=frozenset({"field_declaration"}),
    comment_types=frozenset({"line_comment", "block_comment"}),
    decorator_types=frozenset(),
)

_ECMASCRIPT_COMMON = dict(
    import_types=frozenset({"import_statement"}),
    wrapper_types=frozenset({"export_statement", "ambient_declaration"}),
    variable_types=frozenset({"lexical_declaration", "variable_declaration"}),
    function_value_types=frozenset({"arrow_function", "function_expression", "generator_function", "function"}),
    comment_types=frozenset({"comment"}),
    decorator_types=frozenset({"decorator"}),
)

_JAVASCRIPT = LanguageSpec(
    name="javascript",
    container_kinds={"class_declaration": "class", "class": "class"},
    callable_kinds={
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "method_definition": "method",
    },
    atomic_kinds={},
    field_types=frozenset({"field_definition"}),
    **_ECMASCRIPT_COMMON,
)

_TYPESCRIPT_SHARED = dict(
    container_kinds={
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "class": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
    },
    callable_kinds={
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "function_signature": "function",
        "method_definition": "method",
        "abstract_method_signature": "method",
    },
    atomic_kinds={"type_alias_declaration": "type"},
    field_types=frozenset({"public_field_definition", "property_signature", "method_signature"}),
    **_ECMASCRIPT_COMMON,
)

_TYPESCRIPT = LanguageSpec(name="typescript", **_TYPESCRIPT_SHARED)
_TSX = LanguageSpec(name="tsx", **_TYPESCRIPT_SHARED)

_SPECS = {"java": _JAVA, "javascript": _JAVASCRIPT, "typescript": _TYPESCRIPT, "tsx": _TSX}

_GRAMMARS = {
    "java": tree_sitter_java.language,
    "javascript": tree_sitter_javascript.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
}


def spec_for(language: str) -> LanguageSpec:
    return _SPECS[language]


@lru_cache(maxsize=None)
def parser_for(language: str) -> Parser:
    return Parser(Language(_GRAMMARS[language]()))
