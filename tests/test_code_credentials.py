"""A credential-named variable, property or setter that is given a literal in code. Every value here is made up."""

from __future__ import annotations

import pytest

from repomind.ingest.filters import credential_lines, find_credential
from repomind.parsing.chunker import parse_source
from repomind.parsing.credentials import hidden_lines

FAKE = "fake-value-0123456789"


class TestCodeThatHoldsOne:
    @pytest.mark.parametrize(
        ("language", "line", "name"),
        [
            ("java", f'    private static final String JWT_SECRET = "{FAKE}";', "JWT_SECRET"),
            ("java", f'    String dbPassword = "{FAKE}";', "dbPassword"),
            ("java", f'        this.apiKey = "{FAKE}";', "apiKey"),
            ("java", f'        dataSource.setPassword("{FAKE}");', "setPassword"),
            ("java", f'        return DataSourceBuilder.create().username("app").password("{FAKE}").build();', "password"),
            ("typescript", f"export const API_TOKEN = '{FAKE}';", "API_TOKEN"),
            ("typescript", f"const clientSecret: string = '{FAKE}';", "clientSecret"),
            ("typescript", f"  password: '{FAKE}',", "password"),
            ("typescript", f"const db = {{ user: 'app', password: '{FAKE}' }};", "password"),
            ("javascript", f'  "auth_key": "{FAKE}",', "auth_key"),
            ("javascript", f"function connect(password = '{FAKE}') {{}}", "password"),
            ("tsx", f"const ACCESS_KEY = `{FAKE}`;", "ACCESS_KEY"),
            ("typescript", f"export const PRIVATE_KEY =\n  '{FAKE}';", "PRIVATE_KEY"),
            ("java", f'    // old: String secret = "{FAKE}";', "secret"),
        ],
    )
    def test_it_is_found_and_named_without_its_value(self, language, line, name):
        found = find_credential(line, language)
        assert found == name
        assert FAKE not in str(found)

    def test_a_value_on_the_next_line_hides_both_lines(self):
        text = f"const a = 1;\nexport const PRIVATE_KEY =\n  '{FAKE}';\nconst b = 2;\n"
        assert credential_lines(text, "typescript") == [2, 3]

    def test_a_url_with_a_password_and_a_known_key_format_are_found_in_code_too(self):
        assert find_credential('String url = "postgresql://app:fakepass@db:5432/app";', "java") == "credentials in a URL"
        assert find_credential('const key = "AKIAIOSFODNN7EXAMPLE";', "typescript") == "aws-access-key"


class TestCodeThatDoesNot:
    @pytest.mark.parametrize(
        ("language", "line"),
        [
            # the value comes from somewhere else
            ("typescript", "const password = process.env.DB_PASSWORD;"),
            ("typescript", "  password: dto.password,"),
            ("typescript", "const token = req.cookies[COOKIE_ACCESS];"),
            ("java", "        String secret = properties.security().jwtSecret();"),
            ("java", '    @Value("${stockguard.security.jwt-secret}") String jwtSecret;'),
            ("typescript", "const secret = `${prefix}-${suffix}`;"),
            ("typescript", "const apiKey = '${API_KEY}';"),
            # the name describes a credential, and does not hold one
            ("typescript", "export const COOKIE_ACCESS = 'sn_access';"),
            ("typescript", "export const NOTE_ROLE_KEY = 'syncnotes:noteRole';"),
            ("java", '    static final String TOKEN_HEADER = "Authorization";'),
            ("java", '    static final String TOKEN_PREFIX = "Bearer";'),
            ("typescript", "const tokenType = 'access';"),
            ("typescript", "const PASSWORD_MIN_LENGTH = '8';"),
            ("java", '    private static final String SECRET_NAME = "jwt";'),
            # prose
            ("typescript", "const INVALID_CREDENTIALS = 'Invalid email or password';"),
            ("java", '    static final String PASSWORD_REQUIRED = "password must not be blank";'),
            # not an assignment
            ("typescript", "if (token === 'none') return;"),
            ("java", '        if (password != "x") { }'),
            ("typescript", "const check = (token) => 'ok';"),
            ("typescript", "const shown = isRefresh ? refreshToken : 'none';"),
            ("typescript", "return fetch(url, { credentials: 'include' });"),
            ("typescript", "const value = getToken('refresh');"),
            ("java", '        String claim = jwt.token("role", user);'),
            ("typescript", "interface Login { password: string; }"),
            ("tsx", '<input type="password" autoComplete="current-password" />'),
            ("java", '    @Column(name = "password_hash", nullable = false)'),
            ("typescript", "const password = '';"),
            ("typescript", "const token = 'null';"),
        ],
    )
    def test_it_is_left_alone(self, language, line):
        assert find_credential(line, language) is None
        assert credential_lines(line, language) == []


class TestEachLanguageHasItsOwnRules:
    def test_a_configuration_key_rule_does_not_run_on_code(self):
        line = "  password: hashedPassword,"
        assert find_credential(line, "yaml") == "password"
        assert find_credential(line, "typescript") is None

    def test_configuration_is_judged_as_before(self):
        assert find_credential("  POSTGRES_PASSWORD: fakepass", "yaml") == "POSTGRES_PASSWORD"
        assert find_credential("  password: ${DB_PASSWORD}", "yaml") is None
        assert credential_lines("a: 1\npassword: fakepass\n") == [2]

    def test_sql_is_judged_by_its_own_rule(self):
        assert find_credential("CREATE USER app WITH PASSWORD 'fakepass';", "sql") == "a literal password"
        assert find_credential("ALTER TABLE u ADD COLUMN password TEXT;", "sql") is None

    def test_an_unknown_language_gets_every_rule(self):
        assert find_credential("  password: fakepass", None) == "password"
        assert find_credential(f'JWT_SECRET = "{FAKE}"', None) == "JWT_SECRET"


JAVA = f"""package shop;

class Tokens {{
    private static final String ISSUER = "shop";
    private static final String JWT_SECRET = "{FAKE}";

    String sign(String user) {{
        return Jwts.builder().subject(user).issuer(ISSUER).signWith(JWT_SECRET).compact();
    }}

    boolean valid(String token) {{
        return parser.isSigned(token);
    }}
}}
"""
TYPESCRIPT = f"""export const SETTINGS = {{
  region: 'eu',
  retries: 3,
  apiKey: '{FAKE}',
  timeoutMs: 3000,
  verbose: false,
}};

export function region(): string {{
  return SETTINGS.region;
}}
"""


class TestTheIndexNeverHoldsOne:
    @pytest.mark.parametrize(("path", "language", "text"), [("src/Tokens.java", "java", JAVA), ("src/settings.ts", "typescript", TYPESCRIPT)])
    def test_the_chunk_is_withheld_and_counted_and_the_rest_of_the_file_stays(self, path, language, text):
        parsed = parse_source(path, language, text)
        stored = "\n".join(f"{c.content}\n{c.signature or ''}" for c in parsed.chunks) + "\n".join(s.signature for s in parsed.symbols)
        assert FAKE not in stored
        assert parsed.withheld_chunks >= 1
        assert parsed.chunks, "the file is not dropped for one line"
        for chunk in parsed.chunks:
            assert find_credential(chunk.content, language) is None

    def test_a_signature_that_runs_into_the_value_keeps_only_the_name(self):
        parsed = parse_source("src/settings.ts", "typescript", TYPESCRIPT)
        settings = next(s for s in parsed.symbols if s.name == "SETTINGS")
        assert settings.signature == "SETTINGS"
        assert "region" in {s.name for s in parsed.symbols}

    def test_a_small_class_keeps_every_line_but_the_statement(self):
        parsed = parse_source("src/Tokens.java", "java", JAVA)
        kept = "\n".join(c.content for c in parsed.chunks)
        assert 'ISSUER = "shop"' in kept and "String sign(String user)" in kept and "boolean valid(String token)" in kept
        assert [(c.start_line, c.end_line) for c in parsed.chunks] == [(3, 4), (7, 14)]
        for chunk in parsed.chunks:
            assert chunk.content == "\n".join(JAVA.split("\n")[chunk.start_line - 1 : chunk.end_line]), "still exactly the file's lines"

    def test_a_value_continued_on_the_next_line_goes_with_its_statement(self):
        text = f'class A {{\n    static final String SECRET = "{FAKE}"\n        + "-second-half";\n    int size() {{ return 1; }}\n}}\n'
        parsed = parse_source("A.java", "java", text)
        kept = "\n".join(c.content for c in parsed.chunks)
        assert "second-half" not in kept and FAKE not in kept
        assert "int size()" in kept
        assert hidden_lines("java", text) == [2, 3]

    def test_a_long_statement_loses_only_the_line_with_the_literal(self):
        chain = "\n".join(f'            .option{n}("v{n}")' for n in range(9))
        text = f'class B {{\n    Source build() {{\n        return Builder.create()\n{chain}\n            .password("{FAKE}")\n            .build();\n    }}\n}}\n'
        assert hidden_lines("java", text) == [13]

    def test_a_file_with_no_credential_is_chunked_as_before(self):
        clean = JAVA.replace(f'"{FAKE}"', "loadSecret()")
        parsed = parse_source("src/Tokens.java", "java", clean)
        assert parsed.withheld_chunks == 0
        assert "JWT_SECRET = loadSecret()" in "\n".join(c.content for c in parsed.chunks)
