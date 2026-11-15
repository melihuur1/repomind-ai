"""RepoMind AI: syntax-aware code retrieval with verifiable citations."""

__version__ = "0.1.0"

# The version of what indexing stores for a commit: which lines become chunks, symbols and imports.
# Bump it whenever that output changes for any file. `index` skips a commit already stored under
# the current version, so without a bump an index made by older code is taken for a current one.
# After a bump, running `index` again on a repository reads and parses it again, with no --force.
# Vectors are kept for chunks whose text did not change. tests/test_pipeline_version.py fails when
# the output changes and this number does not.
#
#   2  The first published version.
#   3  A YAML entry holding a credential is taken apart down to the flat list of settings that
#      holds it, and in code the statement that gives a credential-named variable a literal is
#      left out of its chunk. Version 2 also covers indexes made between those two changes.
PIPELINE_VERSION = "3"
