"""Decides which committed files may be indexed.

Path rules run first, on Git metadata alone, so an excluded file's content is
never even read. Content rules then catch binaries, minified bundles and files
that contain high-confidence credentials: a committed secret must not reach the
index or, later, an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FILE_BYTES = 1_000_000
MAX_LINE_LENGTH = 5_000
MAX_AVERAGE_LINE_LENGTH = 500

LANGUAGE_BY_EXTENSION = {
    ".java": "java",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".sql": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
}

EXCLUDED_DIRECTORIES = frozenset(
    {
        "node_modules",
        "bower_components",
        "jspm_packages",
        "vendor",
        "site-packages",
        "venv",
        "target",
        "dist",
        "build",
        "out",
        "coverage",
        "__pycache__",
        "__generated__",
    }
)

GENERATED_SUFFIXES = (".d.ts", ".min.js", ".min.mjs", ".bundle.js", ".chunk.js")

SENSITIVE_FILE_STEMS = frozenset({"secret", "secrets", "credential", "credentials", "creds"})

_SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
}


# Word boundaries inside a file name, so "application-local.yml" matches but "locale.yml" does not.
_PERSONAL_CONFIG = re.compile(
    r"(?:^|[._-])(?:local|personal|private|secrets?|credentials?|creds|vault|kubeconfig|override)(?:[._-]|$)",
    re.IGNORECASE,
)
_LOCK_FILE = re.compile(r"(?:^|[._-])lock(?:[._-]|$)", re.IGNORECASE)
_DATA_DUMP = re.compile(r"(?:^|[._-])(?:dump|backup|bak|seeds?|fixtures?|export)(?:[._-]|$)", re.IGNORECASE)

_SQL_CREDENTIAL = re.compile(r"\b(?:PASSWORD|IDENTIFIED\s+BY)\s+'[^']+'", re.IGNORECASE)

# What a name has to say for its value to be taken for a credential, in configuration and in code alike.
_CREDENTIAL_WORD = r"(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|credential|auth[_-]?key)"
_CONFIG_SECRET_KEY = re.compile(
    rf"^[\s-]*#?\s*[\"']?([\w.-]*{_CREDENTIAL_WORD}[\w.-]*)[\"']?\s*:\s*(.*)$",
    re.IGNORECASE,
)
_CONFIG_BENIGN_KEY = re.compile(
    r"(?:ttl|expir\w*|timeout|length|size|limit|url|uri|path|file|name|header|enabled|interval|lifetime|duration"
    r"|algorithm|issuer|audience|policy|type|prefix)$",
    re.IGNORECASE,
)
# ${NAME}, ${NAME:} and ${NAME:?message} hold no value. ${NAME:-value} and ${NAME:value} do.
_CONFIG_PLACEHOLDER = re.compile(r"^\$\{\w+(?::?\?[^}]*|:-?)?\}$")
_CONFIG_REFERENCE = re.compile(r"^(?:\{\{.*\}\}|<[^>]*>|\*\S+|!\S+.*)$")
_URL_CREDENTIALS = re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@${}]+:[^/\s@${}]+@", re.IGNORECASE)


def find_config_secret(text: str) -> str | None:
    """The name of the first key holding a literal credential. Never the value."""
    for line in text.split("\n"):
        if _URL_CREDENTIALS.search(line):
            return "credentials in a URL"
        match = _CONFIG_SECRET_KEY.match(line)
        if not match or _CONFIG_BENIGN_KEY.search(match.group(1)):
            continue
        value = match.group(2).split(" #", 1)[0].strip().strip("'\"")
        if value.lower() in {"", "null", "~", "true", "false"} or value.isdigit():
            continue
        if _CONFIG_PLACEHOLDER.match(value) or _CONFIG_REFERENCE.match(value):
            continue
        return match.group(1)
    return None


CODE_LANGUAGES = frozenset({"java", "javascript", "typescript", "tsx"})
_CREDENTIAL_NAME = re.compile(_CREDENTIAL_WORD, re.IGNORECASE)
# A credential-named variable, property or setter that is handed a quoted literal:
#   NAME = "..."    NAME: Type = "..."    name: "..."    "name": "..."    setName("...")
# Whitespace before the literal may include a line break, so a value on the next line is seen too.
_CODE_LITERAL = re.compile(
    r"""(?<![\w$])(?P<name>[A-Za-z_$][\w$]*)["']?
        (?P<how>
            \s*(?::\s*[\w.<>\[\]?]+(?:\s*[|&]\s*[\w.<>\[\]?]+)*)?\s*=(?![=>])
          | \s*:(?![:=])
          | \s*\(
        )
        \s*(?P<quote>["'`])(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)""",
    re.VERBOSE,
)
# A setter or a builder step named for exactly a credential: password("..."), setApiKey("...").
_CREDENTIAL_SETTER = re.compile(
    r"^(?:password|passwd|pwd|secret|token|apikey|privatekey|accesskey|secretkey|authkey|credentials?)$"
)
_CODE_REFERENCE = re.compile(r"\$\{|#\{|\{\{|%s|%\(")
# The browser's fetch takes `credentials: "include"`. That names a mode, and holds nothing.
_FETCH_CREDENTIALS_MODES = frozenset({"include", "omit", "same-origin"})


def _is_a_literal_credential(value: str) -> bool:
    """Whether a quoted value could be one. Prose, such as an error message, has spaces, and is not."""
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        return False
    if value.lower() in {"null", "none", "undefined", "true", "false"} or value.isdigit():
        return False
    return not (_CODE_REFERENCE.search(value) or _CONFIG_PLACEHOLDER.match(value) or _CONFIG_REFERENCE.match(value))


def code_credentials(text: str) -> list[tuple[str, int, int]]:
    """Names in code that are given a literal credential, with where each starts and ends in the text.

    The places are character offsets, from the name to the end of the literal. Never the value.
    """
    found = []
    for match in _CODE_LITERAL.finditer(text):
        name, how = match.group("name"), match.group("how").strip()[-1]
        if not _is_a_literal_credential(match.group("value")):
            continue
        if how == "(":
            setter = re.sub(r"^(?:set|with)(?=[A-Z_])", "", name).replace("_", "").lower()
            # Only as the single argument, so that a lookup such as token("refresh", user) is left alone.
            if not _CREDENTIAL_SETTER.match(setter) or text[match.end() :].lstrip()[:1] != ")":
                continue
        elif not _CREDENTIAL_NAME.search(name) or _CONFIG_BENIGN_KEY.search(name):
            continue
        if name.lower() == "credentials" and match.group("value").strip() in _FETCH_CREDENTIALS_MODES:
            continue
        if how == ":":
            # A property or a named argument, and not the middle of `condition ? token : "none"`.
            before = text[: match.start("name")].rstrip(" \t")
            before = before[:-1].rstrip(" \t") if before[-1:] in "\"'" else before
            if before[-1:] not in ("", "\n", "{", ",", "(", "["):
                continue
        found.append((name, match.start("name"), match.end()))
    return found


def _code_credential_lines(text: str) -> list[tuple[str, int, int]]:
    return [(name, text.count("\n", 0, start) + 1, text.count("\n", 0, end) + 1) for name, start, end in code_credentials(text)]


def find_credential(text: str, language: str | None) -> str | None:
    """What in these lines may hold a credential, by the rules of the language they are in. Never the value.

    Every language: a known key format, and a URL with a password. YAML: a credential key with a literal
    value. SQL: a literal password. Code: a credential-named variable, property or setter given a quoted
    literal. An unknown language gets every rule, so that what cannot be told apart is not let through.
    """
    rules = find_secret_rules(text)
    if rules:
        return rules[0]
    if _URL_CREDENTIALS.search(text):
        return "credentials in a URL"
    if language in (None, "yaml"):
        name = find_config_secret(text)
        if name:
            return name
    if language in (None, "sql") and _SQL_CREDENTIAL.search(text):
        return "a literal password"
    if language is None or language in CODE_LANGUAGES:
        found = code_credentials(text)
        if found:
            return found[0][0]
    return None


def credential_lines(text: str, language: str | None = "yaml") -> list[int]:
    """1-indexed lines of a file holding a literal credential, by the rules of its language.

    They are withheld from the index, but the file around them stays readable, so anything that
    shows the whole file has to hide these lines too. For code, ``repomind.parsing.credentials``
    widens each to the whole statement, which this module cannot see.
    """
    hidden: set[int] = set()
    for number, line in enumerate(text.split("\n"), start=1):
        if find_secret_rules(line) or _URL_CREDENTIALS.search(line):
            hidden.add(number)
        elif language in (None, "yaml") and find_config_secret(line):
            hidden.add(number)
        elif language in (None, "sql") and _SQL_CREDENTIAL.search(line):
            hidden.add(number)
    if language is None or language in CODE_LANGUAGES:
        for _name, start, end in _code_credential_lines(text):
            hidden.update(range(start, end + 1))
    return sorted(hidden)


@dataclass(frozen=True)
class PathDecision:
    language: str | None
    skip_reason: str | None

    @property
    def included(self) -> bool:
        return self.skip_reason is None


def classify_path(path: str, size: int = 0) -> PathDecision:
    pure = PurePosixPath(path)
    for part in pure.parts:
        if part.startswith("."):
            return PathDecision(None, "hidden")
    for part in pure.parts[:-1]:
        if part.lower() in EXCLUDED_DIRECTORIES:
            return PathDecision(None, "excluded_directory")

    name = pure.name.lower()
    language = LANGUAGE_BY_EXTENSION.get(pure.suffix.lower())
    name_parts = name.split(".")
    # "prod.env.js" holds values; "env.ts" is usually the schema that validates them.
    if name_parts[0] in SENSITIVE_FILE_STEMS or "env" in name_parts[1:-1]:
        return PathDecision(None, "sensitive_name")
    if language is None:
        return PathDecision(None, "unsupported_language")
    if language == "yaml" and _PERSONAL_CONFIG.search(pure.stem):
        return PathDecision(None, "sensitive_name")
    if language == "yaml" and _LOCK_FILE.search(pure.stem):
        return PathDecision(None, "generated")
    if language == "sql" and _DATA_DUMP.search(pure.stem):
        return PathDecision(None, "data_dump")
    if name.endswith(GENERATED_SUFFIXES):
        return PathDecision(None, "generated")
    if size > MAX_FILE_BYTES:
        return PathDecision(None, "too_large")
    return PathDecision(language, None)


def find_secret_rules(text: str) -> list[str]:
    """Names of the secret rules that match. The secret itself is never returned."""
    return [name for name, pattern in _SECRET_PATTERNS.items() if pattern.search(text)]


def normalize_source(raw: bytes) -> str | None:
    """Decode a blob into the canonical text all line numbers refer to.

    Only ``\\n`` separates lines. That matches both Tree-sitter's row counting and
    ``git show | sed -n``, whereas ``str.splitlines`` would also break on form
    feeds and U+2028 and silently shift every later line number.
    """
    if b"\0" in raw:
        return None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n")


def classify_content(raw: bytes, language: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(text, None)`` when indexable, otherwise ``(None, skip_reason)``."""
    if b"\0" in raw:
        return None, "binary"
    text = normalize_source(raw)
    if text is None:
        return None, "not_utf8"
    lines = text.split("\n")
    if max((len(line) for line in lines), default=0) > MAX_LINE_LENGTH:
        return None, "minified"
    if len(text) / max(len(lines), 1) > MAX_AVERAGE_LINE_LENGTH:
        return None, "minified"
    if find_secret_rules(text):
        return None, "contains_secret"
    if language == "sql" and _SQL_CREDENTIAL.search(text):
        return None, "contains_secret"
    return text, None


_TEST_DIRECTORIES = frozenset({"test", "tests", "__tests__", "__mocks__", "e2e", "spec", "specs"})
_TEST_FILE = re.compile(r"(\.(spec|test|int-spec|e2e-spec)\.[cm]?[jt]sx?$)|((Test|Tests|IT|ITCase)\.java$)")


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if any(part.lower() in _TEST_DIRECTORIES for part in pure.parts[:-1]):
        return True
    return bool(_TEST_FILE.search(pure.name))
