"""Content that looks like a credential is not backed up until the user says it is safe.

Two kinds of rule, run over a file's text line by line:

* known token formats: a vendor prefix and a body of the documented length, a PEM
  private key header, a JWT, a URL with a password in it;
* an assignment whose key names a secret (``api_key = ...``, ``"token": "..."``)
  and whose value is a long, high-entropy string.

A hit records the rule and the line, never the matched text. False positives are
expected (a memory note about token formats), so a hit is a question to the user,
answered per content (allow) or per path (exclude); see ``asset_scan_resolve``.

``CORPUS`` holds, for every rule, text it must catch and text it must leave alone.
The tests run these exact rules over it (a stub must not be looser than production),
and a rule without corpus entries fails them. The samples are assembled from pieces
at import so that no complete credential-shaped string sits in the source.

Standard library only: the backup child process imports this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

# A secret value: long enough, varied enough. Shannon entropy in bits per character.
MIN_SECRET_VALUE_CHARS = 20
MIN_SECRET_ENTROPY = 3.5
# Every pattern is bounded and anchored at a token start, so a line costs time in
# proportion to its length. A line longer than MAX_LINE_CHARS is not matched at all
# but counted as a suspected secret (embedded data is where keys hide), and so is a
# text past MAX_SCAN_CHARS: the user allows it or excludes the file.
MAX_LINE_CHARS = 4096
MAX_SCAN_CHARS = 1_000_000
LONG_LINE = "long_line"
LONG_TEXT = "long_text"
# The variable part of every token pattern is bounded at 512 characters (no real
# credential is longer), so a match attempt never runs to the end of a long line.

_SECRET_KEY_NAMES = (
    r"api[_-]?key|apikey|secret|secret[_-]?key|client[_-]?secret|token|access[_-]?token|"
    r"auth[_-]?token|refresh[_-]?token|bearer|password|passwd|pwd|private[_-]?key|access[_-]?key|"
    r"credential|credentials|session[_-]?key"
)
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9_.-])
    (?P<key>["']?[A-Za-z0-9_.-]{0,40}?(?:""" + _SECRET_KEY_NAMES + r""")["']?)
    \s{0,8}(?::=|=|:)\s{0,8}
    (?P<quote>["']?)(?P<value>[A-Za-z0-9+/_.~=:-]{20,512})(?P=quote)
    """
)


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _looks_random(value: str) -> bool:
    """Long, high entropy, and mixing letters with digits (prose and paths rarely do all three)."""
    if len(value) < MIN_SECRET_VALUE_CHARS or _entropy(value) < MIN_SECRET_ENTROPY:
        return False
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def _assignment_hits(line: str) -> bool:
    return any(_looks_random(match.group("value")) for match in _ASSIGNMENT.finditer(line))


@dataclass(frozen=True)
class SecretRule:
    """One rule: a pattern, or a predicate over the line for the ones a regex cannot decide."""

    id: str
    description: str
    pattern: re.Pattern[str] | None = None
    predicate: Callable[[str], bool] | None = field(default=None, compare=False)

    def hits(self, line: str) -> bool:
        if self.pattern is not None and self.pattern.search(line):
            return True
        return self.predicate is not None and self.predicate(line)


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


RULES: tuple[SecretRule, ...] = (
    SecretRule("anthropic_api_key", "Anthropic API key", _rx(r"\bsk-ant-[A-Za-z0-9_-]{20,512}")),
    SecretRule("openai_api_key", "OpenAI API key",
               _rx(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,512}T3BlbkFJ[A-Za-z0-9_-]{20,512}"
                   r"|\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,512}")),
    SecretRule("github_token", "GitHub token",
               _rx(r"\bgh[pousr]_[A-Za-z0-9]{36,512}|\bgithub_pat_[A-Za-z0-9_]{22,512}")),
    SecretRule("gitlab_token", "GitLab token", _rx(r"\bglpat-[A-Za-z0-9_-]{20,512}")),
    SecretRule("aws_access_key_id", "AWS access key id", _rx(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    SecretRule("google_api_key", "Google API key", _rx(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    SecretRule("slack_token", "Slack token", _rx(r"\bxox[abposr]-[A-Za-z0-9-]{10,512}")),
    SecretRule("stripe_secret_key", "Stripe secret key", _rx(r"\b[rs]k_live_[0-9A-Za-z]{20,512}")),
    SecretRule("huggingface_token", "Hugging Face token", _rx(r"\bhf_[A-Za-z0-9]{30,512}")),
    SecretRule("npm_token", "npm token", _rx(r"\bnpm_[A-Za-z0-9]{36}\b")),
    SecretRule("private_key", "PEM private key",
               _rx(r"-----BEGIN (?:[A-Z0-9]{1,20} ){0,4}PRIVATE KEY(?: BLOCK)?-----")),
    SecretRule("jwt", "JSON Web Token",
               _rx(r"\beyJ[A-Za-z0-9_-]{8,512}\.eyJ[A-Za-z0-9_-]{8,512}\.[A-Za-z0-9_-]{8,512}")),
    SecretRule("url_credentials", "URL with a password",
               _rx(r"(?<![A-Za-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s/:@]{1,256}:[^\s/@]{3,256}@[^\s/]{1,256}")),
    SecretRule("secret_assignment", "a secret-named key set to a long random value",
               predicate=_assignment_hits),
)


@dataclass(frozen=True)
class Hit:
    """A rule that matched a line (1-based)."""

    rule: str
    line: int


def scan_text(text: str, rules: tuple[SecretRule, ...] = RULES) -> list[Hit]:
    """Every (rule, line) hit in ``text``; one hit per rule and line.

    A line over MAX_LINE_CHARS is one ``long_line`` hit, unmatched; past MAX_SCAN_CHARS
    the scan stops with one ``long_text`` hit. Both are suspected secrets.
    """
    hits = []
    scanned = 0
    for number, line in enumerate(text.splitlines(), 1):
        scanned += len(line) + 1
        if scanned > MAX_SCAN_CHARS:
            hits.append(Hit(LONG_TEXT, number))
            break
        if len(line) > MAX_LINE_CHARS:
            hits.append(Hit(LONG_LINE, number))
            continue
        hits.extend(Hit(rule.id, number) for rule in rules if rule.hits(line))
    return hits


def scan_bytes(data: bytes, rules: tuple[SecretRule, ...] = RULES) -> list[Hit]:
    """``scan_text`` over a file's bytes, read as UTF-8 with undecodable bytes replaced."""
    return scan_text(data.decode("utf-8", errors="replace"), rules)


# ----------------------------------------------------------------------------
# Corpus: what each rule must catch and must leave alone.
# ----------------------------------------------------------------------------

_ALPHABET = "Q7mZ2kX9pL4vR8nB3cT6wY1hJ5dF0gS"


def _body(length: int, alphabet: str = _ALPHABET) -> str:
    """A deterministic, varied token body (never a real credential)."""
    return "".join(alphabet[(i * 7 + 3) % len(alphabet)] for i in range(length))


def _join(*parts: str) -> str:
    return "".join(parts)


@dataclass(frozen=True)
class CorpusEntry:
    rule: str
    must_hit: tuple[str, ...]
    must_not_hit: tuple[str, ...]


CORPUS: tuple[CorpusEntry, ...] = (
    CorpusEntry("anthropic_api_key",
                (_join("export KEY=", "sk-", "ant-", "api03-", _body(40)),),
                ("the key starts with sk-ant- followed by the account part",)),
    CorpusEntry("openai_api_key",
                (_join("OPENAI=", "sk-", "proj-", _body(48)),
                 _join("sk-", _body(20), "T3Blbk", "FJ", _body(20))),
                ("sk-learn is a library name", "use sk-proj- prefixed keys")),
    CorpusEntry("github_token",
                (_join("token: ", "gh", "p_", _body(36)), _join("gith", "ub_pat_", _body(30))),
                ("ghp_ tokens are classic personal access tokens", "github_pat_ is the fine-grained prefix")),
    CorpusEntry("gitlab_token", (_join("gl", "pat-", _body(20)),), ("glpat- is GitLab's prefix",)),
    CorpusEntry("aws_access_key_id", (_join("AK", "IA", _body(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")),),
                ("AKIA keys are 20 characters", "AKIAshort")),
    CorpusEntry("google_api_key", (_join("AI", "za", _body(35)),), ("AIza prefixed keys are 39 characters",)),
    CorpusEntry("slack_token", (_join("xo", "xb-", _body(12), "-", _body(12)),), ("xoxb- tokens belong to bots",)),
    CorpusEntry("stripe_secret_key", (_join("sk", "_live_", _body(24)),), ("sk_live_ keys are live mode keys",)),
    CorpusEntry("huggingface_token", (_join("h", "f_", _body(34)),), ("hf_ tokens come from the settings page",)),
    CorpusEntry("npm_token", (_join("np", "m_", _body(36)),), ("npm_config_registry is a variable", "npm_token")),
    CorpusEntry("private_key", (_join("-----BEGIN ", "OPENSSH PRIVATE KEY-----"),
                                _join("-----BEGIN ", "RSA PRIVATE KEY-----")),
                ("-----BEGIN PUBLIC KEY-----", "a private key header starts with five dashes")),
    CorpusEntry("jwt", (_join("Authorization: Bearer ", "eyJ", _body(16), ".eyJ", _body(24), ".", _body(20)),),
                ("tokens starting with eyJ are base64 JSON",)),
    CorpusEntry("url_credentials", (_join("postgres://admin:", _body(12), "@db.internal:5432/app"),),
                ("https://example.com/path@v2", "ssh://git@host/repo.git", "see https://user@host/docs")),
    CorpusEntry("secret_assignment",
                (_join("api_key = \"", _body(32), "\""), _join("\"client_secret\": \"", _body(28), "\""),
                 _join("DB_PASSWORD=", _body(24))),
                ("password: see the vault entry for the staging database",
                 "token_count = 123456789012345678901234",
                 "secret_santa = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                 "api_key_path = /home/someone/projects/keys/readme.md")),
)
