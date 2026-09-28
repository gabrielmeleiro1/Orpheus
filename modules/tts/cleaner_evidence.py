"""Shared word-fragment evidence for OCR repair and artifact scoring."""

from __future__ import annotations

import re


# Ordinary word boundaries need positive evidence even when concatenation matches.
COMMON_WORDS = frozenset("""
a about above after again against all almost along already also although always am
among an and another any anyone anything are around as at away back be because
been before being below between both but by came can cannot come could day did do
does doing done down during each either else enough even ever every everyone
everything few first for from further get give go goes going good got great had
has have having he her here hers herself him himself his how however i if in into
is it its itself just keep kind know last later least less let like little long
look made make many may me might more most much must my myself near need never
new next no nobody none nor not nothing now o of off often old on once one only
or other others our ours ourselves out over own part per perhaps put quite rather
really right said same say says see seem several shall she should since so some
someone something sometimes still such take than that the their theirs them
themselves then there these they thing things think this those though through
thus time times to together too toward two under until up upon us use used using
very wait want was way we well went were what whatever when where whether which while
who whom whose why will with within without would yes yet you your yours yourself
yourselves
""".split())
_SYMBOL_CONTEXT_WORDS = frozenset("""
axis axes coordinate coordinates cubic degree degrees equal equals
equation equations function functions measured meter meters metre metres minus
plus positive negative power radius second seconds square squared symbol symbols
value values variable variables varies vector vectors volt volts watt watts
denotes represents measures approaches tends
""".split())
COMMON_WORDS |= _SYMBOL_CONTEXT_WORDS
BOUND_PREFIXES = frozenset("""anti auto bi co de dis en ex extra hyper inter intra macro
micro mis multi non over post pre pro re semi sub super trans tri ultra un under""".split())
BOUND_SUFFIXES = frozenset("""ing ings tion tions ation ations sion sions ment ments
ness ly ed er ers est ous ful less able ible al ial ity ities ive ize ise ism ist""".split())


def has_fragment_evidence(
    left: str, right: str, *, conservative: bool = False, previous_word: str = "",
) -> bool:
    """Recognize detached letters and affixes without a joined-word dictionary."""
    first, second = left.casefold(), right.casefold()
    if first in COMMON_WORDS and second in COMMON_WORDS:
        return False
    if conservative:
        if not right.islower() or second in COMMON_WORDS:
            return False
        if len(left) == 1:
            return (
                first not in {"a", "i", "o"} and len(right) > 1
                and previous_word.casefold() not in _SYMBOL_CONTEXT_WORDS
            )
        # These short suffixes are also names, interjections or standalone words.
        return (
            second in BOUND_SUFFIXES - {"ed", "er", "al", "able"}
            and len(left) >= 3 and re.fullmatch(r"[A-Z]?[a-z]+", left) is not None
            and re.search(r"[aeiouy]", left, re.IGNORECASE) is not None
        )
    return (
        any(len(word) == 1 and word not in {"a", "i", "o"} for word in (first, second))
        or first in BOUND_PREFIXES or second in BOUND_SUFFIXES
    )
