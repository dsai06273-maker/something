"""Employer email classification and application matching.

Pure functions, standard library only. No network or database access, so the
logic is easy to unit-test and to reason about.

The classifier is deliberately conservative: a wrong auto-update (marking a live
application "rejected") is worse than a missed one, so ambiguous or conflicting
signals resolve to "other" and nothing changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Labels
REJECTED = "rejected"
INTERVIEW = "interview"
OFFER = "offer"
ASSESSMENT = "assessment"
ACKNOWLEDGEMENT = "acknowledgement"
OTHER = "other"

ACTIONABLE_LABELS = (REJECTED, INTERVIEW, OFFER)

# (regex, weight). Applied to lower-cased, whitespace-collapsed text.
_PATTERNS: dict[str, list[tuple[str, float]]] = {
    REJECTED: [
        (r"\b(?:not|no longer) (?:be )?(?:moving|proceeding|going|continuing|advancing) forward", 0.9),
        (r"\bwill not be (?:moving|proceeding|continuing|advancing)", 0.9),
        (r"\bwon'?t be (?:moving|proceeding|continuing|advancing)", 0.9),
        (r"\bdecided to (?:move|proceed|go) forward with (?:other|another|different) ", 0.92),
        (r"\bdecided not to (?:move|proceed|pursue|continue|advance)", 0.9),
        (r"\bother candidates (?:whose|who|that|with)\b", 0.9),
        (r"\b(?:position|role|job|opening) (?:has|had) (?:now )?(?:been )?(?:filled|closed)", 0.88),
        (r"\bnot (?:been )?selected\b", 0.85),
        (r"\b(?:application|candidacy) (?:was|has been|is) unsuccessful", 0.88),
        (r"\b(?:unable|not able) to (?:offer|extend|move forward)", 0.88),
        (r"\b(?:regret|are sorry|am sorry|sorry) to (?:inform|tell|let you know|say)", 0.7),
        (r"\bunfortunately,? (?:we|after|at this)", 0.5),
        (r"\bwe will not be (?:able to )?(?:offer|extend)", 0.88),
    ],
    INTERVIEW: [
        (r"\b(?:invite|invited|invitation)(?: you)? (?:to|for) (?:an? |your |the )?(?:\w+ )?interview", 0.92),
        (r"\b(?:schedule|set up|arrange|book|plan) (?:an?|your|the|a quick|a short) (?:\w+ )?(?:interview|phone screen|video call|call|chat|conversation|screen)\b", 0.85),
        (r"\bwould (?:like|love) to (?:interview|speak with|talk (?:to|with)|meet with|chat with|connect with) you", 0.85),
        (r"\b(?:advance|move|moving|moved) (?:you |your application )?(?:forward )?to the next (?:round|stage|step)", 0.85),
        (r"\b(?:phone|video|technical|onsite|on-site|virtual|first[- ]round) (?:screen|interview)\b", 0.6),
        (r"\b(?:your )?availability (?:for|this|next|on)\b", 0.6),
        (r"\b(?:calendly\.com|goodtime\.io|cal\.com/|savvycal\.com)", 0.6),
        (r"\bselect a (?:time|slot)\b", 0.6),
    ],
    OFFER: [
        (r"\b(?:pleased|delighted|thrilled|excited|happy|glad) to (?:offer|extend)", 0.92),
        (r"\boffer of employment\b", 0.9),
        (r"\bextend (?:you )?(?:an|the|a formal) offer\b", 0.92),
        (r"\bwould (?:like|love) to offer you\b", 0.92),
        (r"\b(?:formal|written|verbal|official) (?:job )?offer\b", 0.85),
        (r"\boffer letter\b", 0.8),
    ],
    ASSESSMENT: [
        (r"\b(?:complete|take|finish|start) (?:the |our |a |an |your )?(?:online )?(?:assessment|coding (?:challenge|test|exercise)|take-?home|skills test)", 0.85),
        (r"\b(?:hackerrank|codility|codesignal|testgorilla|hirevue|karat)\b", 0.7),
    ],
    ACKNOWLEDGEMENT: [
        (r"\bthank(?:s| you) for (?:applying|your (?:application|interest)|submitting)", 0.8),
        (r"\b(?:we(?:'ve| have)|has|have) received your application", 0.85),
        (r"\bapplication (?:has been |was )?(?:successfully )?(?:received|submitted)\b", 0.8),
    ],
}

_COMPILED = {
    label: [(re.compile(rx), w) for rx, w in pats] for label, pats in _PATTERNS.items()
}

# Offer / interview phrases are only trusted when not negated shortly before them.
_NEGATION_TAIL = re.compile(r"(?:\bnot\b|\bunable\b|\bcannot\b|can't|won't|n't\b|\bunfortunately\b|\bno longer\b)[^.!?]{0,30}$")

_BODY_LIMIT = 4000
_MIN_LABEL_CONFIDENCE = 0.6  # weaker evidence than this is treated as "other"


@dataclass(frozen=True)
class Classification:
    label: str
    confidence: float
    matched: tuple[str, ...] = ()


def _normalize_text(text: str) -> str:
    text = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def classify_email(subject: str, body: str) -> Classification:
    """Classify an employer email by its subject and the start of its body."""
    subj = _normalize_text(subject)
    text = f"{subj} . {_normalize_text(body)[:_BODY_LIMIT]}"

    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    for label, patterns in _COMPILED.items():
        found: list[tuple[float, str]] = []
        for rx, weight in patterns:
            for m in rx.finditer(text):
                if label in (OFFER, INTERVIEW) and _NEGATION_TAIL.search(text[max(0, m.start() - 40): m.start()]):
                    continue
                found.append((weight, m.group(0)))
                break  # one hit per pattern is enough
        if found:
            found.sort(reverse=True)
            # strongest pattern + small bonus for each corroborating pattern
            conf = min(0.99, found[0][0] + 0.05 * (len(found) - 1))
            scores[label] = conf
            hits[label] = [h for _, h in found]

    if not scores:
        return Classification(OTHER, 0.0)

    # Explicit rejection language beats softer interview-ish language
    # (e.g. "not moving forward with your application" vs "moving forward").
    rej = scores.get(REJECTED, 0.0)
    if rej >= 0.85 and scores.get(INTERVIEW, 0.0) < 0.9 and scores.get(OFFER, 0.0) < 0.9:
        return Classification(REJECTED, rej, tuple(hits[REJECTED]))

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_label, top_conf = ranked[0]
    if top_label in ACTIONABLE_LABELS and len(ranked) > 1:
        second_label, second_conf = ranked[1]
        if second_label in ACTIONABLE_LABELS and top_conf - second_conf < 0.1:
            return Classification(OTHER, 0.0, tuple(hits[top_label] + hits[second_label]))
    if top_conf < _MIN_LABEL_CONFIDENCE:
        return Classification(OTHER, 0.0, tuple(hits[top_label]))
    return Classification(top_label, top_conf, tuple(hits[top_label]))


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

_PRE_INTERVIEW = {"applied", "manual_required", "reviewed"}
_IN_PROGRESS = _PRE_INTERVIEW | {"interview", "interviewing", "interviewed"}

# Statuses where an incoming email is considered relevant to the application.
OPEN_STATUSES = tuple(sorted(_IN_PROGRESS | {"offer"}))


def next_status(current: str, label: str) -> str | None:
    """Return the status an email should move an application to, or None.

    Never downgrades or overrides terminal/manual states (accepted, withdrawn,
    rejected, offer->rejected) and ignores applications that were never actually
    submitted (error, skipped, saved).
    """
    if label == REJECTED and current in _IN_PROGRESS:
        return "rejected"
    if label == INTERVIEW and current in _PRE_INTERVIEW:
        return "interview"
    if label == OFFER and current in _IN_PROGRESS:
        return "offer"
    return None


# ---------------------------------------------------------------------------
# Application matching
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
    "company", "gmbh", "plc", "pvt", "private", "lp", "llp", "sa", "ag", "bv",
}

# Sender domains that say nothing about which company wrote the email.
_GENERIC_SENDER_DOMAINS = {
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co", "ashbyhq.com",
    "myworkday.com", "workday.com", "icims.com", "smartrecruiters.com", "jobvite.com",
    "linkedin.com", "indeed.com", "indeedemail.com", "gmail.com", "outlook.com",
    "yahoo.com", "hotmail.com", "icloud.com", "workablemail.com", "bamboohr.com",
    "recruitee.com", "teamtailor.com", "breezy.hr", "notifications.hirehive.com",
}


@dataclass(frozen=True)
class Match:
    application_id: int
    score: int
    ambiguous: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _words(text: str) -> str:
    """Lower-case alphanumeric words padded with spaces, for whole-word search."""
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


def normalize_company(name: str) -> str:
    tokens = [t for t in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split() if t]
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _domain_label(addr: str) -> tuple[str, str]:
    """Return (registrable domain, second-level label) for an email address."""
    domain = addr.rsplit("@", 1)[-1].lower().strip(" >")
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac"} and len(parts[-1]) == 2:
        registrable = ".".join(parts[-3:])
        label = parts[-3]
    else:
        registrable = ".".join(parts[-2:])
        label = parts[-2] if len(parts) >= 2 else parts[0]
    return registrable, label


def match_application(
    applications: list[dict],
    sender_name: str,
    sender_addr: str,
    subject: str,
    body: str,
) -> Match | None:
    """Find the application an email most likely refers to.

    `applications` are dicts with at least id, company, job_title.
    Returns None when nothing matches. `Match.ambiguous` is True when two
    different applications tie for the best score.
    """
    name_w = _words(sender_name)
    subj_w = _words(subject)
    body_w = _words((body or "")[:3000])
    registrable, label = _domain_label(sender_addr or "")
    domain_is_generic = registrable in _GENERIC_SENDER_DOMAINS

    scored: list[Match] = []
    for app in applications:
        company = normalize_company(app.get("company", ""))
        if len(company) < 2:
            continue
        needle = f" {company} "
        nospace = company.replace(" ", "")
        score = 0
        reasons: list[str] = []

        if not domain_is_generic and (
            label == nospace or (len(nospace) >= 5 and nospace in label)
        ):
            score += 3
            reasons.append("sender domain")
        if needle in name_w:
            score += 2
            reasons.append("sender name")
        if needle in subj_w:
            score += 2
            reasons.append("subject")
        if needle in body_w:
            score += 1
            reasons.append("body")

        if score == 0:
            continue  # job title alone is never enough

        title = normalize_company(app.get("job_title", ""))  # same normalisation is fine
        if len(title) >= 6:
            t_needle = f" {title} "
            if t_needle in subj_w:
                score += 2
                reasons.append("title in subject")
            elif t_needle in body_w:
                score += 1
                reasons.append("title in body")

        scored.append(Match(int(app["id"]), score, False, tuple(reasons)))

    if not scored:
        return None
    scored.sort(key=lambda m: m.score, reverse=True)
    best = scored[0]
    if len(scored) > 1 and scored[1].score == best.score:
        return Match(best.application_id, best.score, True, best.reasons)
    return best
