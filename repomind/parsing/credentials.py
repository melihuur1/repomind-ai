"""The lines of a file that are neither indexed nor shown, because a credential is on them.

``repomind.ingest.filters`` finds where one is, by the rules of the file's language. In code that
is a credential-named variable, property or setter given a literal, and Tree-sitter then widens
it to the whole statement, so that the second half of a value written over two lines,
``"part1" + "part2"``, is not left behind.
"""

from __future__ import annotations

from repomind.ingest.filters import CODE_LANGUAGES, code_credentials, credential_lines
from repomind.parsing.languages import parser_for

# One whole thing to leave out, besides whatever is named a statement or a declaration.
_MEMBERS = frozenset({"pair", "public_field_definition", "field_definition", "property_signature", "element_value_pair", "enum_constant"})
# Never widened to: leaving out a class or a function for one line would hide what the index is for.
_CONTAINERS = frozenset(
    {
        "program", "class_body", "statement_block", "block", "object", "interface_body", "enum_body", "switch_block",
        "class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
        "annotation_type_declaration", "method_declaration", "constructor_declaration", "compact_constructor_declaration",
        "function_declaration", "generator_function_declaration", "method_definition", "arrow_function", "function_expression",
        "if_statement", "for_statement", "for_in_statement", "enhanced_for_statement", "while_statement", "do_statement",
        "try_statement", "switch_statement", "labeled_statement", "export_statement",
    }
)
# A longer statement, such as a builder chain, keeps its other lines: only the line with the literal goes.
_MAX_STATEMENT_LINES = 8


def _statement(node):
    while node is not None:
        if node.type in _CONTAINERS:
            return None
        if node.type in _MEMBERS or node.type.endswith(("_statement", "_declaration")):
            return node
        node = node.parent
    return None


def hidden_lines(language: str | None, text: str) -> list[int]:
    """1-indexed lines holding a credential, each widened to its statement where the language is code."""
    lines = set(credential_lines(text, language))
    if language in CODE_LANGUAGES:
        found = code_credentials(text)
        if found:
            root = parser_for(language).parse(text.encode("utf-8")).root_node
            for _name, start, end in found:
                first, last = len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8"))
                statement = _statement(root.descendant_for_byte_range(first, last))
                if statement is not None and statement.end_point[0] - statement.start_point[0] < _MAX_STATEMENT_LINES:
                    lines.update(range(statement.start_point[0] + 1, statement.end_point[0] + 2))
    return sorted(lines)
