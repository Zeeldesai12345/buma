from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from buma.schemas.normalized_event import IssueRef
from buma.worker.services.category_rules import CATEGORY_KEYWORD_MAP, CATEGORY_LABEL_MAP
from buma.worker.services.priority_rules import PRIORITY_KEYWORDS, PRIORITY_LABEL_MAP, PRIORITY_ORDER

if TYPE_CHECKING:
    from buma.worker.services.claude_client import ClaudeClassifier
    from buma.worker.services.llm_budget import LLMBudget

logger = logging.getLogger(__name__)

ENGINE_VERSION = "rules-v1"
# Claude answered and its response passed validation.
CLAUDE_ENGINE_VERSION = "claude-hybrid-v1"
# Rule confidence was low and Claude was attempted but failed/timed out/returned invalid data —
# the rule result was used anyway.
FALLBACK_ENGINE_VERSION = "rules-v1-fallback"
# Rule confidence was low but Claude was skipped by the cost gate (daily budget exhausted or
# circuit breaker open) — the rule result was used. Kept distinct from FALLBACK_ENGINE_VERSION
# so "skipped over budget" and "Claude failed" can be counted separately.
BUDGET_ENGINE_VERSION = "rules-v1-budget"

DEFAULT_CATEGORY = "bug"
DEFAULT_PRIORITY = "P2"
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_CLAUDE_MAX_PRIORITY = "P1"


@dataclass(frozen=True)
class TriageResult:
    category: str
    priority: str
    confidence: float
    engine_version: str
    # Optional human-readable note appended to the GitHub explanation comment
    # (e.g. when a Claude-suggested priority was capped).
    note: str | None = None


class TriageEngine:
    """
    Classifies a GitHub issue into a category and priority using
    deterministic rules (label matching → keyword matching → fallback).

    `classify()` is pure logic — no I/O, no DB, no async. Rules are defined in
    category_rules.py and priority_rules.py.

    `classify_with_fallback()` layers an optional hybrid step on top: when the rule result's
    confidence is below `confidence_threshold`, it asks the injected `ClaudeClassifier` to
    classify instead. If no classifier is configured, or Claude fails/times out/returns invalid
    data, it always falls back to the rule result — this method never raises.

    Guardrails on the Claude path:
    - `llm_budget` (optional) gates every Claude call on a per-repo daily budget and a circuit
      breaker; a refused call returns the rule result with `BUDGET_ENGINE_VERSION`.
    - `claude_max_priority` is a severity ceiling: a Claude-only answer more severe than it is
      capped, because a valid-but-injected P0 would page people. Rule-engine results are never capped.
    """

    def __init__(
        self,
        claude_classifier: ClaudeClassifier | None = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        llm_budget: LLMBudget | None = None,
        claude_max_priority: str = DEFAULT_CLAUDE_MAX_PRIORITY,
    ) -> None:
        if claude_max_priority not in PRIORITY_ORDER:
            raise ValueError(f"claude_max_priority must be one of {PRIORITY_ORDER}, got {claude_max_priority!r}")
        self._claude_classifier = claude_classifier
        self._confidence_threshold = confidence_threshold
        self._llm_budget = llm_budget
        self._claude_max_priority = claude_max_priority

    def classify(self, issue: IssueRef, config: dict) -> TriageResult:
        text = self._build_text(issue.title, issue.body)
        category, cat_confidence = self._classify_category(issue.labels, text, config)
        priority, pri_confidence = self._classify_priority(issue.labels, text, config)
        non_zero = [c for c in (cat_confidence, pri_confidence) if c > 0]
        confidence = min(non_zero) if non_zero else 0.0

        logger.debug(
            "category=%s(%.2f) priority=%s(%.2f) overall=%.2f",
            category,
            cat_confidence,
            priority,
            pri_confidence,
            confidence,
        )

        return TriageResult(
            category=category,
            priority=priority,
            confidence=confidence,
            engine_version=ENGINE_VERSION,
        )

    async def classify_with_fallback(
        self,
        issue: IssueRef,
        config: dict,
        repo_id: int | None = None,
        event_id: str | None = None,
    ) -> TriageResult:
        """
        Rule-based classification first; consult Claude only when confidence is low.

        Never raises: any exception from the Claude classifier is caught here as a second
        safety net (the classifier's own `classify()` is already expected not to raise), and
        the rule-based result is returned instead.
        """
        rule_result = self.classify(issue, config)

        if self._claude_classifier is None or rule_result.confidence >= self._confidence_threshold:
            return rule_result

        if self._llm_budget is not None and repo_id is not None:
            if not await self._llm_budget.allow(repo_id):
                logger.info(
                    "event_id=%s issue=#%d — Claude skipped by cost gate (rule confidence=%.2f), using rule result",
                    event_id,
                    issue.number,
                    rule_result.confidence,
                )
                return replace(rule_result, engine_version=BUDGET_ENGINE_VERSION)

        try:
            claude_result = await self._claude_classifier.classify(issue, event_id=event_id)
        except Exception:
            logger.exception(
                "event_id=%s issue=#%d — Claude classifier raised unexpectedly, falling back to rule result",
                event_id,
                issue.number,
            )
            claude_result = None

        if self._llm_budget is not None:
            await self._llm_budget.record(ok=claude_result is not None)

        if claude_result is None:
            logger.info(
                "event_id=%s issue=#%d — Claude fallback unavailable/invalid (rule confidence=%.2f), using rule result",
                event_id,
                issue.number,
                rule_result.confidence,
            )
            return replace(rule_result, engine_version=FALLBACK_ENGINE_VERSION)

        claude_result = self._apply_severity_ceiling(claude_result, event_id, issue.number)

        logger.info(
            "event_id=%s issue=#%d — Claude fallback used: rule confidence=%.2f -> category=%s priority=%s "
            "claude_confidence=%.2f",
            event_id,
            issue.number,
            rule_result.confidence,
            claude_result.category,
            claude_result.priority,
            claude_result.confidence,
        )
        return claude_result

    def _apply_severity_ceiling(self, result: TriageResult, event_id: str | None, issue_number: int) -> TriageResult:
        ceiling = self._claude_max_priority
        if PRIORITY_ORDER.index(result.priority) >= PRIORITY_ORDER.index(ceiling):
            return result
        logger.warning(
            "event_id=%s issue=#%d — Claude suggested %s, capped at %s (severity ceiling)",
            event_id,
            issue_number,
            result.priority,
            ceiling,
        )
        return replace(
            result,
            priority=ceiling,
            note=f"Priority capped at {ceiling} — AI-suggested {result.priority} needs a human to confirm.",
        )

    def _classify_category(self, labels: list[str], text: str, config: dict) -> tuple[str, float]:
        label_map = self._merge_maps(CATEGORY_LABEL_MAP, config.get("label_map", {}).get("categories", {}))

        for label in labels:
            category = label_map.get(label.lower())
            if category:
                return category, 1.0

        for phrases, category, confidence in CATEGORY_KEYWORD_MAP:
            if any(phrase in text for phrase in phrases):
                return category, confidence

        default = config.get("defaults", {}).get("category", DEFAULT_CATEGORY)
        return default, 0.0

    def _classify_priority(self, labels: list[str], text: str, config: dict) -> tuple[str, float]:
        label_map = self._merge_maps(PRIORITY_LABEL_MAP, config.get("label_map", {}).get("priorities", {}))

        for label in labels:
            priority = label_map.get(label.lower())
            if priority:
                return priority, 1.0

        matched: list[str] = []
        for priority, phrases in PRIORITY_KEYWORDS.items():
            if any(phrase in text for phrase in phrases):
                matched.append(priority)

        if matched:
            best = min(matched, key=lambda p: PRIORITY_ORDER.index(p))
            confidence = 0.9 if best in ("P0", "P1") else 0.7
            return best, confidence

        default = config.get("defaults", {}).get("priority", DEFAULT_PRIORITY)
        return default, 0.0

    @staticmethod
    def _build_text(title: str, body: str | None) -> str:
        return (title + " " + (body or "")).lower()

    @staticmethod
    def _merge_maps(global_map: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
        merged = dict(global_map)
        merged.update({k.lower(): v for k, v in overrides.items()})
        return merged
