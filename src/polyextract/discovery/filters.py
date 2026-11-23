"""Explainable, deterministic Stage-1 metadata filter.

Decision rules (evaluated in order; the first matching rule decides):

  R0 publication identity -> a web-search-only hit with no DOI is REJECTED if its URL is a known
     non-scholarly site (Wikipedia, Q&A, vendor pages); otherwise it can be at most REVIEW.
  R1 hard document type   -> REJECT  (erratum, retraction, editorial, review, ...)
  R2 year outside range   -> REJECT
  R3 no polymer evidence  -> REJECT
  R4 ACCEPT when polymer AND (molecular_weight OR mwd) AND property,
     with no off_topic / simulation_only / semicrystalline-without-amorphous flags.
  R5 REVIEW when the R4 signals are present but a soft flag fired, OR the paper is polymer plus at
     least one of {molecular_weight, mwd, property}, OR the abstract is missing (a title alone is
     weak evidence, so the paper is not rejected for lacking an abstract).
  R6 otherwise            -> REJECT

Unknown document types and missing years are not rejected; they are flagged in the reasons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from polyextract.discovery.models import Decision, DiscoveryCandidate, FilterDecision, RuleResult, TermMatch

GROUPS_REQUIRED_FOR_ACCEPT = ("polymer", "property")
MW_GROUPS = ("molecular_weight", "mwd")


@dataclass
class _Pattern:
    group: str
    label: str
    regex: re.Pattern


class FilterEngine:
    def __init__(self, config: dict):
        self.config = config
        self.version: str = config["version"]
        self.year_range: tuple[int, int] = tuple(config.get("year_range", (1900, 2100)))  # type: ignore[assignment]
        self.allowed_types = {t.lower() for t in config.get("allowed_document_types", [])}
        self.hard_excluded_types = {t.lower() for t in config.get("hard_excluded_document_types", [])}
        self.fields: list[str] = config.get("fields", ["title", "abstract", "keywords", "snippet"])
        self.weights: dict[str, float] = config.get("weights", {})
        self.non_scholarly = [s.lower() for s in config.get("non_scholarly_url_patterns", [])]
        self.patterns: list[_Pattern] = []
        for group, pats in config["term_groups"].items():
            for p in pats:
                if isinstance(p, dict):
                    src, cs = p["re"], bool(p.get("cs", False))
                else:
                    src, cs = p, False
                self.patterns.append(_Pattern(group, src, re.compile(src, 0 if cs else re.IGNORECASE)))

    @classmethod
    def from_yaml(cls, path: Path) -> "FilterEngine":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    # ------------------------------------------------------------------------------------
    def _field_texts(self, cand: DiscoveryCandidate) -> dict[str, str]:
        p = cand.paper
        texts = {
            "title": p.title or "",
            "abstract": p.abstract or "",
            "keywords": " ; ".join(p.keywords),
            "snippet": " ... ".join(cand.snippets),
        }
        return {k: v for k, v in texts.items() if k in self.fields and v}

    def match_terms(self, cand: DiscoveryCandidate) -> list[TermMatch]:
        matches: list[TermMatch] = []
        for fname, text in self._field_texts(cand).items():
            for pat in self.patterns:
                m = pat.regex.search(text)
                if m:
                    matches.append(TermMatch(group=pat.group, term=pat.label, field=fname, text=m.group(0)))
        return matches

    def score(self, groups: set[str]) -> float:
        return round(sum(self.weights.get(g, 0.0) for g in groups), 3)

    def evaluate(self, cand: DiscoveryCandidate) -> FilterDecision:
        p = cand.paper
        matches = self.match_terms(cand)
        groups = {m.group for m in matches}
        rules: list[RuleResult] = []
        reasons: list[str] = []

        def finish(decision: Decision) -> FilterDecision:
            return FilterDecision(
                filter_version=self.version,
                decision=decision,
                score=self.score(groups),
                needs_review=decision is Decision.REVIEW,
                groups_matched=sorted(groups),
                matches=matches,
                rules=rules,
                reasons=reasons,
            )

        identity_unverified = not p.doi and bool(p.retrieval_sources) and set(p.retrieval_sources) <= {"websearch"}
        url = (p.source_url or "").lower()
        non_scholarly = identity_unverified and any(s in url for s in self.non_scholarly)
        rules.append(
            RuleResult(
                rule_id="R0",
                description="identifiable scholarly publication (DOI or scholarly source)",
                passed=not identity_unverified,
                detail=f"doi={bool(p.doi)} sources={p.retrieval_sources} non_scholarly_url={non_scholarly}",
            )
        )
        if non_scholarly:
            reasons.append(f"Rejected: non-scholarly web page without DOI ({p.source_url}).")
            return finish(Decision.REJECT)

        dtype = (p.document_type or "").lower()
        hard = dtype in self.hard_excluded_types
        rules.append(RuleResult(rule_id="R1", description="document type not hard-excluded", passed=not hard, detail=dtype or "unknown"))
        if hard:
            reasons.append(f"Rejected: document type '{dtype}' is excluded.")
            return finish(Decision.REJECT)
        if dtype and self.allowed_types and dtype not in self.allowed_types:
            reasons.append(f"Note: document type '{dtype}' is not in the allowed list (not rejected; flagged).")

        y0, y1 = self.year_range
        year_ok = p.publication_year is None or (y0 <= p.publication_year <= y1)
        rules.append(RuleResult(rule_id="R2", description=f"year within [{y0}, {y1}]", passed=year_ok, detail=str(p.publication_year)))
        if not year_ok:
            reasons.append(f"Rejected: publication year {p.publication_year} outside [{y0}, {y1}].")
            return finish(Decision.REJECT)
        if p.publication_year is None:
            reasons.append("Note: publication year unknown.")

        has_polymer = "polymer" in groups
        rules.append(RuleResult(rule_id="R3", description="polymer terminology present", passed=has_polymer))
        if not has_polymer:
            reasons.append("Rejected: no polymer terminology in title/abstract/keywords/snippet.")
            return finish(Decision.REJECT)

        has_mw = any(g in groups for g in MW_GROUPS)
        has_prop = "property" in groups
        soft_flags = []
        if "off_topic" in groups:
            soft_flags.append("off-topic indicator (biological/environmental)")
        if "simulation_only" in groups:
            soft_flags.append("possibly simulation-only (no experimental measurements)")
        if "semicrystalline" in groups and "amorphous" not in groups:
            soft_flags.append("semicrystalline indicators without amorphous indicators")
        if identity_unverified:
            soft_flags.append("publication identity unverified (web hit without DOI)")
        core = has_polymer and has_mw and has_prop
        rules.append(
            RuleResult(
                rule_id="R4",
                description="polymer AND (molecular_weight OR mwd) AND property, no soft flags",
                passed=core and not soft_flags,
                detail=f"mw={has_mw} property={has_prop} flags={soft_flags}",
            )
        )
        matched_desc = ", ".join(sorted(groups))
        if core and not soft_flags:
            reasons.append(f"Accepted: matched groups [{matched_desc}].")
            return finish(Decision.ACCEPT)

        no_abstract = not p.abstract
        partial = has_mw or has_prop
        to_review = (core and soft_flags) or partial or no_abstract
        rules.append(
            RuleResult(
                rule_id="R5",
                description="partial match / soft flag / abstract missing -> human review",
                passed=bool(to_review),
                detail=f"partial={partial} soft_flags={bool(soft_flags)} abstract_missing={no_abstract}",
            )
        )
        if to_review:
            if core and soft_flags:
                reasons.append("Review: core signals present but " + "; ".join(soft_flags) + ".")
            elif partial:
                missing = [n for n, ok in (("molecular-weight/MWD", has_mw), ("property", has_prop)) if not ok]
                reasons.append(f"Review: polymer paper missing {', '.join(missing)} terms in metadata.")
            if no_abstract:
                reasons.append("Review: abstract unavailable; title-only evidence is insufficient to reject.")
            return finish(Decision.REVIEW)

        rules.append(RuleResult(rule_id="R6", description="fallback reject", passed=False))
        reasons.append("Rejected: polymer paper without molecular-weight or property terminology.")
        return finish(Decision.REJECT)
