"""Which declaration a constant's name means where it is used.

The same name is declared more than once in most of these: in another function, another block,
another file or another package. The question is always which one a given line means, and the
answer has to be that one or nothing.
"""

from __future__ import annotations

import pytest

from repomind.qa.declarations import resolve
from tests.test_completion import Files


def line_of(source: str, marker: str) -> int:
    found = [number for number, line in enumerate(source.split("\n"), start=1) if marker in line]
    assert len(found) == 1, f"{marker!r} is on {len(found)} lines"
    return found[0]


def means(files: Files, name: str, path: str, marker: str):
    """Where the name used on the marked line is declared, as (path, line), or the reason there is nothing."""
    line = line_of(files.files[path][1], marker)
    declaration, reason = resolve(files, name, path, line, line)
    return (declaration.path, declaration.start_line) if declaration else reason


def one(language: str, source: str, path: str = "") -> Files:
    return Files({path or {"java": "A.java", "javascript": "a.js", "tsx": "a.tsx"}.get(language, "a.ts"): (language, source)})


NOT_FOUND = "no declaration of {} was found"

SCOPES = """\
export const MAX_LEN = 200; // top

function shadowed(text: string) {
  const MAX_LEN = 5; // local
  return text.slice(0, MAX_LEN); // use-local
}

function plain(text: string) {
  return text.slice(0, MAX_LEN); // use-top
}

function parameter(text: string, MAX_LEN: number) {
  return text.slice(0, MAX_LEN); // use-parameter
}

function closure() {
  const MAX_LEN = 7; // enclosing
  return (text: string) => text.slice(0, MAX_LEN); // use-enclosing
}

function block(flag: boolean, text: string) {
  if (flag) {
    const MAX_LEN = 9; // in-block
    return text.slice(0, MAX_LEN); // use-in-block
  }
  return text.slice(0, MAX_LEN); // use-after-block
}
"""


class TestTypeScriptScope:
    def test_a_constant_of_another_function_is_not_what_a_name_means(self):
        source = "function first() {\n  const MAX_LEN = 5;\n  return MAX_LEN;\n}\n\nexport function second(text: string) {\n  return text.slice(0, MAX_LEN); // use\n}\n"
        assert means(one("typescript", source), "MAX_LEN", "a.ts", "// use") == NOT_FOUND.format("MAX_LEN")

    @pytest.mark.parametrize(
        ("use", "declared_at"),
        [
            ("use-local", "// local"),
            ("use-top", "// top"),
            ("use-enclosing", "// enclosing"),
            ("use-in-block", "// in-block"),
            ("use-after-block", "// top"),
        ],
    )
    def test_the_nearest_declaration_around_the_use_is_the_one(self, use, declared_at):
        assert means(one("typescript", SCOPES), "MAX_LEN", "a.ts", use) == ("a.ts", line_of(SCOPES, declared_at))

    def test_a_parameter_hides_the_constant_and_is_not_followed(self):
        assert means(one("typescript", SCOPES), "MAX_LEN", "a.ts", "use-parameter") == NOT_FOUND.format("MAX_LEN")

    def test_lines_where_the_name_means_two_things_are_refused(self):
        files = one("typescript", SCOPES)
        declaration, reason = resolve(files, "MAX_LEN", "a.ts", line_of(SCOPES, "use-local"), line_of(SCOPES, "use-top"))
        assert declaration is None and reason == "MAX_LEN means 2 different declarations in these lines"

    def test_var_belongs_to_the_function_and_const_to_the_block(self):
        source = (
            "function f(flag: boolean, text: string) {\n  if (flag) {\n    const NEW_LEN = 5;\n    var OLD_LEN = 9;\n  }\n"
            "  return text.slice(NEW_LEN, OLD_LEN); // use\n}\n"
        )
        files = one("typescript", source)
        assert means(files, "OLD_LEN", "a.ts", "// use") == ("a.ts", 4)
        assert means(files, "NEW_LEN", "a.ts", "// use") == NOT_FOUND.format("NEW_LEN")

    @pytest.mark.parametrize(
        "source",
        [
            "const { MAX_LEN } = config;\nexport const a = MAX_LEN; // use\n",
            "const { size: MAX_LEN } = config;\nexport const a = MAX_LEN; // use\n",
            "const [MAX_LEN] = sizes;\nexport const a = MAX_LEN; // use\n",
            "const MAX_LEN = 1;\nfor (const MAX_LEN of sizes) {\n  check(MAX_LEN); // use\n}\n",
            "const MAX_LEN = 1;\ntry {\n  run();\n} catch (MAX_LEN) {\n  report(MAX_LEN); // use\n}\n",
            "const MAX_LEN = 1;\nexport const f = (MAX_LEN) => MAX_LEN + 1; // use\n",
            "const MAX_LEN = 1;\nexport const f = MAX_LEN => MAX_LEN + 1; // use\n",
            "const MAX_LEN = 1;\nexport function f({ MAX_LEN }: Options) {\n  return MAX_LEN; // use\n}\n",
            "function MAX_LEN() {\n  return 1;\n}\nexport const a = MAX_LEN(); // use\n",
            "enum MAX_LEN { A }\nexport const a = MAX_LEN.A; // use\n",
            "declare const MAX_LEN: number;\nexport const a = MAX_LEN; // use\n",
            "let MAX_LEN;\nexport const a = MAX_LEN; // use\n",
            "import MAX_LEN from './limits';\nexport const a = MAX_LEN; // use\n",
            "import * as MAX_LEN from './limits';\nexport const a = MAX_LEN; // use\n",
            "const MAX_LEN = 1;\nexport const a = config.MAX_LEN; // use\n",
            "const MAX_LEN = 1;\nexport const a = MAX_LEN + config.MAX_LEN; // use\n",
            "class Limits {\n  static MAX_LEN = 5;\n  size() {\n    return MAX_LEN; // use\n  }\n}\n",
            "const MAX_LEN = 1;\nexport const a = { MAX_LEN: 2 }; // use\n",
            "const MAX_LEN = 1;\n// MAX_LEN and 'MAX_LEN' // use\nexport const a = 'MAX_LEN';\n",
        ],
    )
    def test_what_is_not_a_plain_constant_in_scope_is_never_followed(self, source):
        files = Files({"a.ts": ("typescript", source), "limits.ts": ("typescript", "export const MAX_LEN = 5;\nexport default 5;\n")})
        assert means(files, "MAX_LEN", "a.ts", "// use") == NOT_FOUND.format("MAX_LEN")

    @pytest.mark.parametrize(
        ("language", "source", "line"),
        [
            ("typescript", "const MAX_LEN = 1;\nexport const a = { MAX_LEN }; // use\n", 1),
            ("typescript", "const MAX_LEN = 1;\nexport function f(size = MAX_LEN) { // use\n  return size;\n}\n", 1),
            ("typescript", "const MAX_LEN = 1;\nexport { MAX_LEN }; // use\n", 1),
            ("typescript", "export const a = () => MAX_LEN; // use\nconst MAX_LEN = 1;\n", 2),
            ("typescript", "export const MAX_LEN = 1; // use\n", 1),
            ("javascript", "var MAX_LEN = 1;\nmodule.exports = () => MAX_LEN; // use\n", 1),
            ("tsx", "const MAX_LEN = 5;\nexport const Field = () => <input maxLength={MAX_LEN} />; // use\n", 1),
        ],
    )
    def test_a_plain_constant_in_scope_is_followed(self, language, source, line):
        files = one(language, source)
        path = next(iter(files.files))
        assert means(files, "MAX_LEN", path, "// use") == (path, line)


PUBLISHER = """\
export class Publisher implements OnDestroy {
  private static readonly MAX_LEN = 1000; // static
  readonly TTL_MS = 5; // instance
  static NO_VALUE: number;

  publish(text: string) {
    return text.slice(0, Publisher.MAX_LEN); // use-own-class
  }
  wait() {
    return this.TTL_MS + Publisher.TTL_MS + Publisher.NO_VALUE; // use-not-static
  }
  shadowed(Publisher: Other) {
    return Publisher.MAX_LEN; // use-shadowed
  }
}

export class Reader {
  static readonly MAX_LEN = 20; // reader
  read(text: string) {
    return text.slice(0, Reader.MAX_LEN) + Publisher.MAX_LEN; // use-two-classes
  }
}

export const outside = Publisher.MAX_LEN; // use-outside
"""


class TestTypeScriptStaticFields:
    @pytest.mark.parametrize(("use", "declared_at"), [("use-own-class", "// static"), ("use-outside", "// static")])
    def test_a_static_field_is_followed_through_the_name_of_a_class_the_file_declares(self, use, declared_at):
        assert means(one("typescript", PUBLISHER), "MAX_LEN", "a.ts", use) == ("a.ts", line_of(PUBLISHER, declared_at))

    def test_an_instance_field_a_field_with_no_value_and_this_are_not_followed(self):
        files = one("typescript", PUBLISHER)
        assert means(files, "TTL_MS", "a.ts", "use-not-static") == NOT_FOUND.format("TTL_MS")
        assert means(files, "NO_VALUE", "a.ts", "use-not-static") == NOT_FOUND.format("NO_VALUE")

    def test_the_line_that_declares_a_static_field_means_that_field_and_a_plain_name_never_does(self):
        assert means(one("typescript", PUBLISHER), "MAX_LEN", "a.ts", "// static") == ("a.ts", line_of(PUBLISHER, "// static"))
        source = "const MAX_LEN = 1; // top\nclass A {\n  static MAX_LEN = MAX_LEN + 1;\n  static size() {\n    return MAX_LEN; // use\n  }\n}\n"
        assert means(one("typescript", source), "MAX_LEN", "a.ts", "// use") == ("a.ts", 1)

    def test_a_parameter_named_like_the_class_hides_it(self):
        assert means(one("typescript", PUBLISHER), "MAX_LEN", "a.ts", "use-shadowed") == NOT_FOUND.format("MAX_LEN")

    def test_the_same_field_name_in_two_classes_on_one_line_is_refused(self):
        assert means(one("typescript", PUBLISHER), "MAX_LEN", "a.ts", "use-two-classes") == "MAX_LEN means 2 different declarations in these lines"

    def test_a_class_from_another_file_is_not_followed_and_a_same_named_one_is_never_used(self):
        user = "import { Publisher } from './publisher';\nexport const a = Publisher.MAX_LEN; // use\nexport const b = Unknown.MAX_LEN; // unknown\n"
        files = Files({"a.ts": ("typescript", user), "publisher.ts": ("typescript", PUBLISHER)})
        assert means(files, "MAX_LEN", "a.ts", "// use") == NOT_FOUND.format("MAX_LEN")
        assert means(files, "MAX_LEN", "a.ts", "// unknown") == NOT_FOUND.format("MAX_LEN")

    def test_javascript_class_fields_are_read_the_same_way(self):
        source = "class Publisher {\n  static MAX_LEN = 1000;\n  size = 2;\n  f() {\n    return Publisher.MAX_LEN; // use\n  }\n}\n"
        assert means(one("javascript", source), "MAX_LEN", "a.js", "// use") == ("a.js", 2)


USER = "import { MAX_LEN } from './limits';\nexport const short = (text: string) => text.slice(0, MAX_LEN); // use\n"


class TestTypeScriptImports:
    def files(self, **extra):
        return Files({
            "web/title.ts": ("typescript", USER),
            "web/limits.ts": ("typescript", "function helper() {\n  const MAX_LEN = 1;\n  return MAX_LEN;\n}\nexport const MAX_LEN = 200;\n"),
            "admin/title.ts": ("typescript", USER),
            "admin/limits.ts": ("typescript", "export const MAX_LEN = 999;\n"),
            **extra,
        })

    def test_the_same_name_in_two_files_is_followed_to_the_one_each_file_imports(self):
        files = self.files()
        assert means(files, "MAX_LEN", "web/title.ts", "// use") == ("web/limits.ts", 5), "the top level, not the helper's own"
        assert means(files, "MAX_LEN", "admin/title.ts", "// use") == ("admin/limits.ts", 1)

    def test_a_same_named_constant_in_a_file_it_does_not_import_is_never_used(self):
        source = "import { MAX_LEN } from '@acme/shared';\nexport const a = MAX_LEN; // use\n"
        files = self.files(**{"web/other.ts": ("typescript", source)})
        assert means(files, "MAX_LEN", "web/other.ts", "// use") == "MAX_LEN is imported from @acme/shared, which is not a file of this repository"

    def test_an_import_under_another_name_is_followed_to_the_name_it_has_there(self):
        source = "import { MAX_LEN as TITLE_LEN } from './limits';\nexport const a = TITLE_LEN; // use\n"
        files = self.files(**{"web/other.ts": ("typescript", source)})
        assert means(files, "TITLE_LEN", "web/other.ts", "// use") == ("web/limits.ts", 5)
        assert means(files, "MAX_LEN", "web/other.ts", "import {") == NOT_FOUND.format("MAX_LEN"), "this file has nothing called MAX_LEN"

    def test_the_import_line_itself_is_followed(self):
        assert means(self.files(), "MAX_LEN", "web/title.ts", "import {") == ("web/limits.ts", 5)

    @pytest.mark.parametrize(
        "target",
        [
            "export function size() {\n  const MAX_LEN = 5;\n  return MAX_LEN;\n}\n",
            "export { MAX_LEN } from './real';\n",
            "export * from './real';\n",
            "import { MAX_LEN } from './real';\nexport { MAX_LEN };\n",
        ],
    )
    def test_a_module_that_does_not_declare_it_at_its_top_level_is_not_searched_further(self, target):
        files = self.files(**{"web/limits.ts": ("typescript", target), "web/real.ts": ("typescript", "export const MAX_LEN = 1;\n")})
        assert means(files, "MAX_LEN", "web/title.ts", "// use") == "MAX_LEN is imported from ./limits, which does not declare it itself"

    def test_a_module_that_could_be_two_files_is_refused(self):
        files = self.files(**{"web/limits/index.ts": ("typescript", "export const MAX_LEN = 3;\n")})
        assert means(files, "MAX_LEN", "web/title.ts", "// use") == "MAX_LEN is imported from ./limits, which could be 2 files"

    def test_typescript_imported_under_its_output_name_is_found(self):
        source = "import { MAX_LEN } from './limits.js';\nexport const a = MAX_LEN; // use\n"
        files = self.files(**{"web/other.ts": ("typescript", source)})
        assert means(files, "MAX_LEN", "web/other.ts", "// use") == ("web/limits.ts", 5)

    def test_a_local_declaration_hides_the_import(self):
        source = "import { MAX_LEN } from './limits';\nexport function f() {\n  const MAX_LEN = 3;\n  return MAX_LEN; // use\n}\n"
        files = self.files(**{"web/other.ts": ("typescript", source)})
        assert means(files, "MAX_LEN", "web/other.ts", "// use") == ("web/other.ts", 3)


SERVICE = """\
package shop;

class Service extends Base {
    private static final int MAX_RETRIES = 3; // field

    int plain() {
        return MAX_RETRIES; // use-field
    }

    int own() {
        return this.MAX_RETRIES; // use-this
    }

    int local() {
        int MAX_RETRIES = 9; // local
        return MAX_RETRIES; // use-local
    }

    int before() {
        int first = MAX_RETRIES; // use-before-local
        int MAX_RETRIES = 1;
        return first;
    }

    int parameter(int MAX_RETRIES) {
        return MAX_RETRIES; // use-parameter
    }

    int loop() {
        for (int MAX_RETRIES = 0; ; ) {
            return MAX_RETRIES; // use-loop
        }
    }

    int lambda() {
        return apply(MAX_RETRIES -> MAX_RETRIES + 1); // use-lambda
    }

    int pattern(Object value) {
        if (value instanceof Integer MAX_RETRIES) {
            return MAX_RETRIES; // use-pattern
        }
        return 0;
    }

    int elsewhere() {
        int LOCAL_LIMIT = 1;
        return LOCAL_LIMIT;
    }

    int other() {
        return LOCAL_LIMIT; // use-other-method
    }

    @Retry(times = MAX_RETRIES) // use-annotation
    void annotated() { }

    static class Nested {
        int f() {
            return MAX_RETRIES; // use-nested
        }
    }

    static class Own {
        static final int MAX_RETRIES = 1; // own
        int f() {
            return MAX_RETRIES; // use-own
        }
    }

    static class Child extends Parent {
        int f() {
            return MAX_RETRIES; // use-child
        }
    }
}
"""


class TestJavaPlainNames:
    @pytest.mark.parametrize(
        ("use", "declared_at"),
        [
            ("use-field", "// field"),
            ("use-this", "// field"),
            ("use-local", "// local"),
            ("use-before-local", "// field"),
            ("use-annotation", "// field"),
            ("use-nested", "// field"),
            ("use-own", "// own"),
        ],
    )
    def test_a_name_means_the_nearest_local_variable_or_field_around_it(self, use, declared_at):
        assert means(one("java", SERVICE), "MAX_RETRIES", "A.java", use) == ("A.java", line_of(SERVICE, declared_at))

    @pytest.mark.parametrize("use", ["use-parameter", "use-loop", "use-lambda", "use-pattern"])
    def test_a_parameter_or_a_variable_of_a_loop_a_lambda_or_a_pattern_hides_the_field(self, use):
        assert means(one("java", SERVICE), "MAX_RETRIES", "A.java", use) == NOT_FOUND.format("MAX_RETRIES")

    def test_a_local_variable_of_another_method_is_not_what_a_name_means(self):
        assert means(one("java", SERVICE), "LOCAL_LIMIT", "A.java", "use-other-method") == NOT_FOUND.format("LOCAL_LIMIT")

    def test_a_type_that_extends_another_may_inherit_the_name_so_the_outer_field_is_not_taken(self):
        assert means(one("java", SERVICE), "MAX_RETRIES", "A.java", "use-child") == NOT_FOUND.format("MAX_RETRIES")

    def test_a_constant_of_an_interface_it_implements_is_left_alone(self):
        user = "package shop;\nclass Cart implements Limits {\n    boolean full(int n) { return n >= MAX_ITEMS; } // use\n}\n"
        holder = "package shop;\ninterface Limits {\n    int MAX_ITEMS = 50;\n    default boolean over(int n) { return n > MAX_ITEMS; } // inside\n}\n"
        files = Files({"shop/Cart.java": ("java", user), "shop/Limits.java": ("java", holder)}, types={"Limits": ["shop/Limits.java"]})
        assert means(files, "MAX_ITEMS", "shop/Cart.java", "// use") == NOT_FOUND.format("MAX_ITEMS")
        assert means(files, "MAX_ITEMS", "shop/Limits.java", "// inside") == ("shop/Limits.java", 3)

    def test_an_enum_value_is_not_a_field_and_a_field_of_the_enum_is(self):
        source = "enum Level {\n    LOW_LEVEL, HIGH_LEVEL;\n    static final int TOP_LEVEL = 3;\n    int rank() { return TOP_LEVEL + LOW_LEVEL.ordinal(); } // use\n}\n"
        files = one("java", source)
        assert means(files, "TOP_LEVEL", "A.java", "// use") == ("A.java", 3)
        assert means(files, "LOW_LEVEL", "A.java", "// use") == NOT_FOUND.format("LOW_LEVEL")

    def test_a_name_in_a_string_or_a_comment_is_not_a_use(self):
        source = 'class A {\n    static final int MAX_ITEMS = 5;\n    // MAX_ITEMS is five\n    String s = "MAX_ITEMS"; // use\n}\n'
        files = one("java", source)
        assert means(files, "MAX_ITEMS", "A.java", "// use") == NOT_FOUND.format("MAX_ITEMS")
        assert resolve(files, "MAX_ITEMS", "A.java", 3, 3) == (None, NOT_FOUND.format("MAX_ITEMS"))

    def test_a_static_import_is_followed_to_the_type_it_names(self):
        user = "package shop;\nimport static shop.limits.Limits.MAX_ITEMS;\nclass Cart {\n    boolean full(int n) { return n >= MAX_ITEMS; } // use\n}\n"
        files = packages(user)
        assert means(files, "MAX_ITEMS", "shop/Cart.java", "// use") == ("shop/limits/Limits.java", 3)
        library = user.replace("shop.limits.Limits", "org.library.Limits")
        assert means(packages(library), "MAX_ITEMS", "shop/Cart.java", "// use") == NOT_FOUND.format("MAX_ITEMS")

    def test_an_unsupported_language_declares_nothing(self):
        files = Files({"a.yml": ("yaml", "MAX_ITEMS: 5\n")})
        assert resolve(files, "MAX_ITEMS", "a.yml", 1, 1) == (None, NOT_FOUND.format("MAX_ITEMS"))
        assert resolve(files, "MAX_ITEMS", "missing.ts", 1, 1) == (None, "missing.ts is not indexed")


def limits(package: str, value: int) -> str:
    return f"package {package};\npublic class Limits {{\n    public static final int MAX_ITEMS = {value};\n}}\n"


def cart(*lines: str) -> str:
    return "package shop;\n" + "".join(f"{line}\n" for line in lines) + "class Cart {\n    boolean full(int n) { return n >= Limits.MAX_ITEMS; } // use\n}\n"


def packages(user: str, **more) -> Files:
    """Three types called Limits, in three packages, one of them the user's own."""
    sources = {
        "shop/Cart.java": ("java", user),
        "shop/Limits.java": ("java", limits("shop", 5)),
        "shop/limits/Limits.java": ("java", limits("shop.limits", 50)),
        "shop/stock/Limits.java": ("java", limits("shop.stock", 500)),
        **more,
    }
    return Files(sources, types={"Limits": [path for path in sources if path.endswith("Limits.java")]})


class TestJavaQualifiedNames:
    def test_with_no_import_the_type_of_the_same_package_is_meant(self):
        assert means(packages(cart()), "MAX_ITEMS", "shop/Cart.java", "// use") == ("shop/Limits.java", 3)

    @pytest.mark.parametrize(("imported", "path"), [("shop.limits.Limits", "shop/limits/Limits.java"), ("shop.stock.Limits", "shop/stock/Limits.java")])
    def test_an_import_of_the_type_decides_and_hides_the_same_package(self, imported, path):
        assert means(packages(cart(f"import {imported};")), "MAX_ITEMS", "shop/Cart.java", "// use") == (path, 3)

    def test_an_import_of_a_type_outside_the_repository_ends_the_search(self):
        files = packages(cart("import org.library.Limits;", "import shop.stock.*;"))
        assert means(files, "MAX_ITEMS", "shop/Cart.java", "// use") == NOT_FOUND.format("MAX_ITEMS"), "three types of the repository have that name, and none is it"

    def test_the_same_package_comes_before_an_on_demand_import(self):
        assert means(packages(cart("import shop.stock.*;")), "MAX_ITEMS", "shop/Cart.java", "// use") == ("shop/Limits.java", 3)

    def test_an_on_demand_import_is_followed_when_nothing_nearer_has_the_type(self):
        user = cart("import shop.stock.*;").replace("package shop;", "package web;")
        assert means(packages(user), "MAX_ITEMS", "shop/Cart.java", "// use") == ("shop/stock/Limits.java", 3)

    def test_two_on_demand_imports_that_both_have_the_type_are_refused(self):
        user = cart("import shop.stock.*;", "import shop.limits.*;").replace("package shop;", "package web;")
        assert means(packages(user), "MAX_ITEMS", "shop/Cart.java", "// use") == "2 types are called Limits"

    def test_a_type_of_another_package_that_is_not_imported_is_never_taken(self):
        user = cart().replace("package shop;", "package web;")
        assert means(packages(user), "MAX_ITEMS", "shop/Cart.java", "// use") == NOT_FOUND.format("MAX_ITEMS")

    def test_two_files_declaring_the_same_type_are_refused(self):
        files = packages(cart("import shop.limits.Limits;"), **{"copy/shop/limits/Limits.java": ("java", limits("shop.limits", 51))})
        assert means(files, "MAX_ITEMS", "shop/Cart.java", "// use") == "2 types are called shop.limits.Limits"

    def test_a_type_nested_where_the_name_is_used_comes_before_an_import(self):
        user = (
            "package shop;\nimport shop.limits.Limits;\nclass Cart {\n    static class Limits {\n        static final int MAX_ITEMS = 3; // nested\n    }\n"
            "    boolean full(int n) { return n >= Limits.MAX_ITEMS; } // use\n}\n"
        )
        assert means(packages(user), "MAX_ITEMS", "shop/Cart.java", "// use") == ("shop/Cart.java", 5)

    def test_a_type_nested_in_another_type_is_not_in_scope_by_its_short_name(self):
        other = "package shop;\nclass Other {\n    static class Limits {\n        static final int MAX_ITEMS = 7;\n    }\n}\n"
        user = cart("import shop.limits.Limits;")
        files = packages(user, **{"shop/Other.java": ("java", other)})
        files._types["Limits"].append("shop/Other.java")
        assert means(files, "MAX_ITEMS", "shop/Cart.java", "// use") == ("shop/limits/Limits.java", 3)

    def test_a_field_the_type_does_not_declare_itself_is_not_followed(self):
        enum = "package shop.limits;\npublic enum Limits {\n    MAX_ITEMS, MIN_ITEMS\n}\n"
        inherits = "package shop.limits;\npublic class Limits extends Base {\n}\n"
        for holder in (enum, inherits):
            files = packages(cart("import shop.limits.Limits;"), **{"shop/limits/Limits.java": ("java", holder)})
            assert means(files, "MAX_ITEMS", "shop/Cart.java", "// use") == NOT_FOUND.format("MAX_ITEMS")

    @pytest.mark.parametrize("written", ["shop.limits.Limits.MAX_ITEMS", "limits.MAX_ITEMS", "limits().MAX_ITEMS", "Outer.Limits.MAX_ITEMS"])
    def test_a_longer_path_or_a_variable_before_the_name_is_not_followed(self, written):
        user = cart("import shop.limits.Limits;").replace("Limits.MAX_ITEMS", written)
        assert means(packages(user), "MAX_ITEMS", "shop/Cart.java", "// use") == NOT_FOUND.format("MAX_ITEMS")

    def test_the_type_the_name_is_used_in_is_itself_found(self):
        source = "package shop;\nclass Limits {\n    static final int MAX_ITEMS = 5;\n    boolean full(int n) { return n >= Limits.MAX_ITEMS; } // use\n}\n"
        assert means(one("java", source), "MAX_ITEMS", "A.java", "// use") == ("A.java", 3)

    def test_a_field_of_a_nested_class_is_not_what_the_outer_class_means(self):
        source = (
            "class Simulation {\n    static class Inner { static final int REQUEST_TIMEOUT = 5; }\n    static final int REQUEST_TIMEOUT = 60;\n"
            "    int reserve() { return REQUEST_TIMEOUT; } // use\n    int inner() { return Inner.REQUEST_TIMEOUT; } // qualified\n}\n"
        )
        files = one("java", source)
        assert means(files, "REQUEST_TIMEOUT", "A.java", "// use") == ("A.java", 3)
        assert means(files, "REQUEST_TIMEOUT", "A.java", "// qualified") == ("A.java", 2)
