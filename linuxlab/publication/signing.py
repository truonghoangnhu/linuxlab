"""Ed25519 signing primitives for .llpkg packages (ADR-010, Task 11).

A thin wrapper around the `cryptography` package's own Ed25519
implementation -- never a hand-rolled/manual signature scheme. Private
signing keys are never read, generated, stored, or logged here: every
function takes its key as an explicit argument supplied by the caller,
and nothing in this module persists key material anywhere.
"""

SIGNATURE_ALGORITHM = "ed25519"


def sign_bytes(message, private_key):
    """Sign `message` with an Ed25519 private key, returning the raw
    64-byte signature. Deterministic for a given (message, key) pair --
    an inherent Ed25519 property, not something this wrapper adds.
    """
    return private_key.sign(message)


def verify_signature(message, signature, public_key):
    """Verify `signature` over `message` against an Ed25519 public key.

    Raises cryptography.exceptions.InvalidSignature on failure. Left
    untranslated here deliberately: this module is a thin, library-
    transparent primitive, not the public domain boundary -- callers that
    need a package-specific exception (e.g. verify_package()) translate at
    their own boundary instead.
    """
    public_key.verify(signature, message)
