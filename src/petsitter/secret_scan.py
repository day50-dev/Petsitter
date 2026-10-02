"""Finding secrets in text, for Secrets Protector.

No one detector covers what people paste into a chat, so this combines:

- gitleaks' rules (data/gitleaks.toml, MIT, from github.com/gitleaks/gitleaks):
  ~200 maintained, tested patterns for specific vendors' keys and tokens.
- detect-secrets' keyword detector (Yelp): a value by the name it sits under,
  `"password": "..."`, `api_key = '...'`, in most languages' syntax, while
  ignoring code like `password = os.environ[...]`. This is what catches a
  human-chosen password, which the vendor patterns, tuned to random-looking
  keys, never will. Plus its basic-auth detector for user:pass@host URLs.
- our own patterns: unquoted `.env` and YAML lines (`DB_PASSWORD=...`,
  `password: ...`), which detect-secrets only reads when it knows the file
  type, a few vendor formats, and personal details (email, phone, SSN, card
  number). Not IP addresses: an address carries meaning (local or public,
  which subnet, which machine) that a stand-in would destroy.

gitleaks' generic-api-key rule is left out: it flags any random-looking value
after words like "key" or "auth", which snags hashes and encoded data; the
keyword detector covers values that are named as secrets.

find_secrets(text) returns non-overlapping (start, end, value, kind) spans,
earliest and then longest first. Results are cached by text, since a client
resends the whole conversation with every request.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import warnings
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:   # Python 3.10
    import tomli as tomllib   # type: ignore[no-redef]

log = logging.getLogger("petsitter")

GITLEAKS_RULES = Path(__file__).parent / "data" / "gitleaks.toml"
GITLEAKS_SKIP = {"generic-api-key"}

# Names a credential is kept under, for the unquoted .env / YAML pattern:
# password, DB_PASSWORD, client_secret, api_key, GITHUB_TOKEN... It has to
# end in one of these, so max_tokens or token_count don't count.
_CREDENTIAL_KEY = (r"[\w.-]*?(?:password|passwd|pwd|passphrase|secret|secret[_-]?key|"
                   r"api[_-]?key|apikey|access[_-]?key|auth[_-]?token|token)")

# (pattern, kind). A pattern with a group named "v" hides only that group.
OWN_PATTERNS: list[tuple[re.Pattern, str]] = [
    # unquoted, at the start of a line: DB_PASSWORD=x, export TOKEN=x,
    # password: x. "=" with no spaces (shell) or ": " (YAML), which keeps
    # Python's `password = getpass()` out. Skips $VARS, quotes (the keyword
    # detector has those), YAML block markers and type annotations.
    # The value runs to the end of the line (YAML: "password: two words"),
    # less a trailing " # comment".
    (re.compile(rf"(?im)^[ \t]*(?:export[ \t]+)?{_CREDENTIAL_KEY}(?:=|:[ \t]+)"
                r"(?![$\"'|>!&*{\[])(?!(?:str|int|bool|float|bytes|None|null|true|false|Optional)\b)"
                r"(?P<v>[^\s#\"'](?:[^\n#]*[^\s#])?)(?=[ \t]*(?:#|$))"), "credential"),
    # A JSON-style quoted key holding a quoted string: {"db_password": "two
    # words"}. detect-secrets catches this too; this way it's caught even
    # without it. Escaped quotes in the value are kept; ${VARS} are skipped.
    (re.compile(rf'(?i)"{_CREDENTIAL_KEY}"\s*:\s*"(?!\$\{{)(?P<v>(?:[^"\\\n]|\\.)+)"'), "credential"),
    # The name and the value as sibling fields: {"key": "password", "value":
    # "..."}, Kubernetes' {"name": "DB_PASSWORD", "value": "..."}, either order,
    # and the same in YAML (name: DB_PASSWORD / value: ...).
    (re.compile(rf'(?i)"(?:key|name|field|var|variable|setting)"\s*:\s*"{_CREDENTIAL_KEY}"\s*,\s*'
                r'"(?:value|val|data)"\s*:\s*"(?!\$\{)(?P<v>(?:[^"\\\n]|\\.)+)"'), "credential"),
    (re.compile(rf'(?i)"(?:value|val|data)"\s*:\s*"(?!\$\{{)(?P<v>(?:[^"\\\n]|\\.)+)"\s*,\s*'
                rf'"(?:key|name|field|var|variable|setting)"\s*:\s*"{_CREDENTIAL_KEY}"'), "credential"),
    (re.compile(rf"(?im)^[ \t]*-?[ \t]*(?:key|name):[ \t]*[\"']?{_CREDENTIAL_KEY}[\"']?[ \t]*\n"
                r"[ \t]*value:[ \t]*[\"']?(?![$|>])(?P<v>[^\s#\"'](?:[^\n#\"']*[^\s#\"'])?)"), "credential"),
    # vendor formats gitleaks has no rule for, or whose rule Python can't compile
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "openai_proj_key"),
    (re.compile(r"(?<![\w-])sk-(?!proj-|ant-)[A-Za-z0-9]{20,}"), "openai_key"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "anthropic_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_key"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "jwt"),
    (re.compile(r"(?:ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36}"), "github_token"),
    (re.compile(r"Bearer\s+(?P<v>[A-Za-z0-9\-_.=]{30,})"), "bearer_token"),
    (re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss)://[^\s'\"<>]+"), "database_url"),
    (re.compile(r"-----BEGIN\s+(?:[A-Z]+\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:[A-Z]+\s+)?PRIVATE\s+KEY-----"),
     "private_key"),
    # personal details
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email"),
    (re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "phone"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    (re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"), "credit_card"),
]


# -- gitleaks ------------------------------------------------------------------

@dataclass
class _Allow:
    regexes: list[re.Pattern] = field(default_factory=list)
    target: str = "secret"   # or "match", "line"
    stopwords: tuple[str, ...] = ()

    def allows(self, secret: str, match: str, line: str) -> bool:
        target = {"match": match, "line": line}.get(self.target, secret)
        if any(rx.search(target) for rx in self.regexes):
            return True
        low = secret.lower()
        return any(w in low for w in self.stopwords)


@dataclass
class _Rule:
    id: str
    regex: re.Pattern
    group: int
    entropy: float
    keywords: tuple[str, ...]
    allows: list[_Allow]


def _compile_all(patterns) -> list[re.Pattern]:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p))
        except re.error:
            pass
    return out


def _allow(cfg: dict) -> _Allow:
    return _Allow(_compile_all(cfg.get("regexes")), cfg.get("regexTarget", "secret"),
                  tuple(w.lower() for w in cfg.get("stopwords") or ()))


def _load_gitleaks(path: Path = GITLEAKS_RULES) -> tuple[list[_Rule], list[_Allow]]:
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("secrets: can't read gitleaks rules at %s: %s", path, e)
        return [], []
    global_allows = [_allow(a) for a in ([cfg["allowlist"]] if "allowlist" in cfg else [])
                     + list(cfg.get("allowlists") or [])]
    rules, skipped = [], 0
    for r in cfg.get("rules") or []:
        if r.get("id") in GITLEAKS_SKIP or "regex" not in r:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)   # Go-isms like "[[" in a class
                rx = re.compile(r["regex"])
        except re.error:
            skipped += 1   # Go regex syntax Python doesn't take
            continue
        rules.append(_Rule(
            id=r["id"], regex=rx, group=int(r.get("secretGroup") or 0),
            entropy=float(r.get("entropy") or 0.0),
            keywords=tuple(k.lower() for k in r.get("keywords") or ()),
            allows=[_allow(a) for a in r.get("allowlists") or []],
        ))
    log.debug("secrets: %d gitleaks rules loaded, %d skipped", len(rules), skipped)
    return rules, global_allows


def _shannon(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _line_at(text: str, start: int, end: int) -> str:
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    return text[a: b if b >= 0 else len(text)]


def _gitleaks_spans(text: str, rules: list[_Rule], global_allows: list[_Allow]) -> list[tuple]:
    low = text.lower()
    spans = []
    for rule in rules:
        if rule.keywords and not any(k in low for k in rule.keywords):
            continue
        for m in rule.regex.finditer(text):
            g = rule.group
            if not g and m.re.groups:
                # gitleaks' default: the first group that matched
                g = next((i for i in range(1, m.re.groups + 1) if m.group(i)), 0)
            secret = m.group(g)
            if not secret or (rule.entropy and _shannon(secret) < rule.entropy):
                continue
            line = _line_at(text, m.start(), m.end())
            if any(a.allows(secret, m.group(0), line) for a in rule.allows + global_allows):
                continue
            spans.append((m.start(g), m.end(g), secret, "gitleaks:" + rule.id))
    return spans


# -- detect-secrets ------------------------------------------------------------

# Cheap test before handing a line to detect-secrets: the names its keyword
# detector looks for (its DENYLIST), and "://" for its basic-auth detector.
_DS_HINT = re.compile(r"(?i)(?:api|auth|service|account|db|database|priv|private|client)_?key|"
                      r"(?:db|database|key)_?pass|password|passwd|pwd|secret|contrase|://")
_DS_MAX_LINE = 2000   # longer lines are scanned in windows around each hint
_ds_lock = threading.Lock()
_ds_ready: bool | None = None


def _ds_setup() -> bool:
    """Point detect-secrets at the two detectors we use, once. Its settings
    are process-wide, and petsitter is their only user."""
    global _ds_ready
    if _ds_ready is None:
        try:
            from detect_secrets.settings import configure_settings_from_baseline, cache_bust
            cache_bust()
            configure_settings_from_baseline({"plugins_used": [
                {"name": "KeywordDetector"}, {"name": "BasicAuthDetector"}]})
            _ds_ready = True
        except Exception as e:   # not installed, or an API change
            log.warning("secrets: detect-secrets unavailable (%s); passwords in code aren't "
                        "caught. pip install detect-secrets", e)
            _ds_ready = False
    return _ds_ready


def problems() -> list[str]:
    """What's missing from the detectors, for the dashboard."""
    global _rules
    out = []
    with _ds_lock:
        ok = _ds_setup()
    if not ok:
        out.append("detect-secrets isn't installed, so passwords and secrets in code "
                   "(`api_key = '...'`) and some config formats aren't caught; JSON, .env and "
                   "YAML still are. Install it with `pip install detect-secrets` and restart "
                   "petsitter.")
    if _rules is None:
        _rules = _load_gitleaks()
    if not _rules[0]:
        out.append(f"The gitleaks rules at {GITLEAKS_RULES} couldn't be read, so most vendor "
                   "API keys and tokens aren't caught. Reinstalling petsitter restores them.")
    return out


def _ds_pieces(text: str):
    """(offset, line) pieces of text worth scanning."""
    pos = 0
    for line in text.split("\n"):
        if _DS_HINT.search(line):
            if len(line) <= _DS_MAX_LINE:
                yield pos, line
            else:
                for m in _DS_HINT.finditer(line):
                    a = max(0, m.start() - 200)
                    yield pos + a, line[a: m.end() + 400]
        pos += len(line) + 1


def _detect_secrets_spans(text: str) -> list[tuple]:
    if not _DS_HINT.search(text):
        return []
    with _ds_lock:
        if not _ds_setup():
            return []
        from detect_secrets.core.scan import scan_line
        spans = []
        for offset, piece in _ds_pieces(text):
            try:
                found = list(scan_line(piece))
            except Exception:
                continue
            for s in found:
                value = s.secret_value or ""
                i = piece.find(value) if len(value) >= 3 else -1
                while i >= 0:
                    spans.append((offset + i, offset + i + len(value), value, "credential"))
                    i = piece.find(value, i + len(value))
        return spans


# -- all together --------------------------------------------------------------

_rules: tuple[list[_Rule], list[_Allow]] | None = None
_cache: OrderedDict[str, list[tuple]] = OrderedDict()
_CACHE_SIZE = 4096
_cache_lock = threading.Lock()


def _own_spans(text: str) -> list[tuple]:
    spans = []
    for rx, kind in OWN_PATTERNS:
        g = "v" if "v" in rx.groupindex else 0
        for m in rx.finditer(text):
            if m.group(g):
                spans.append((m.start(g), m.end(g), m.group(g), kind))
    return spans


def find_secrets(text: str) -> list[tuple[int, int, str, str]]:
    """Non-overlapping (start, end, value, kind) for every secret in text."""
    global _rules
    if not text:
        return []
    key = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    if _rules is None:
        _rules = _load_gitleaks()
    spans = _own_spans(text) + _gitleaks_spans(text, *_rules) + _detect_secrets_spans(text)
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged, last_end = [], 0
    for s in spans:
        if s[0] >= last_end:
            merged.append(s)
            last_end = s[1]
    with _cache_lock:
        _cache[key] = merged
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return merged
