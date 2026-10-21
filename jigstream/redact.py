"""Strip private data from JSON/text before it is copied out of the app (e.g. to share a sample)."""
import re

PATTERNS = [
    (re.compile(r"https?://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+", re.I), "[webhook-url]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[number]"),
    (re.compile(r"\b[\w-]{24}\.[\w-]{6}\.[\w-]{27,}\b"), "[token]"),
    (re.compile(r'("(?:password|pass|cvv|cvc|token|authorization)"\s*:\s*)"[^"]*"', re.I), r'\1"[redacted]"'),
]


def redact(text):
    for pat, rep in PATTERNS:
        text = pat.sub(rep, text)
    return text
