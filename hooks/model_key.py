# SPDX-License-Identifier: MIT
"""Normalize a model id to a family key (haiku, sonnet, opus, fable).

Unknown ids come back lower-cased and unchanged. The code does not guess.
"""

_FAMILIES = ("fable", "opus", "sonnet", "haiku")


def model_key(s):
    s = (s or "").strip().lower()
    if not s:
        return ""
    for fam in _FAMILIES:
        if fam in s:
            return fam
    return s
