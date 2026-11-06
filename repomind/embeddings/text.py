"""What exactly gets embedded for a chunk."""

from __future__ import annotations

import hashlib

# Changing the format below changes every hash, so all vectors are recomputed.
TEMPLATE_VERSION = "1"


def embedding_text(
    path: str,
    kind: str,
    symbol: str | None,
    signature: str | None,
    content: str,
    part_index: int = 1,
) -> str:
    """Prefix the code with where it lives.

    A later part of a split function does not contain the signature, so without
    this context it would not say what it belongs to.
    """
    header = [f"File: {path}"]
    if symbol:
        header.append(f"{kind} {symbol}")
    if signature and part_index > 1:
        header.append(f"Signature: {signature}")
    return "\n".join(header) + "\n\n" + content


def text_hash(text: str) -> str:
    return hashlib.sha256(f"{TEMPLATE_VERSION}\0{text}".encode("utf-8")).hexdigest()
