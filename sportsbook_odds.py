"""Small, fail-closed adapter for The Odds API's decimal head-to-head odds."""

from __future__ import annotations

import os
import re
import statistics
import time
import json
from typing import Dict, Iterable, Optional

import requests


class SportsbookOddsClient:
    """Fetch no-vig consensus probabilities from configured sportsbook regions."""

    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or os.getenv("ODDS_API_KEY", "")).strip()
        self.sport_keys = [item.strip() for item in os.getenv("ODDS_API_SPORT_KEYS", "").split(",")
                           if item.strip()]
        self.regions = os.getenv("ODDS_API_REGIONS", "uk,eu").strip()
        self.max_age_seconds = max(30, int(os.getenv("ODDS_MAX_AGE_SECONDS", "90")))
        self.min_bookmakers = max(1, int(os.getenv("ODDS_MIN_BOOKMAKERS", "2")))
        try:
            self.team_aliases = json.loads(os.getenv("ODDS_TEAM_ALIASES", "{}"))
            if not isinstance(self.team_aliases, dict):
                self.team_aliases = {}
        except json.JSONDecodeError:
            self.team_aliases = {}
        self.timeout = max(2, float(os.getenv("ODDS_API_TIMEOUT_SECONDS", "10")))
        self.session = requests.Session()
        self._cache = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.sport_keys and self.regions)

    def get_event_odds(self, bayse_event: dict) -> list[dict]:
        """Fetch configured H2H feed events matching this Bayse event's teams."""
        if not self.configured:
            return []
        names = _bayse_team_names(bayse_event)
        if len(names) < 2:
            return []
        bayse_start = _bayse_start_time(bayse_event)
        matches = []
        for sport_key in self.sport_keys:
            now = time.time()
            cached_at, payload = self._cache.get(sport_key, (0, None))
            if payload is None or now - cached_at >= 30:
                try:
                    response = self.session.get(
                        f"{self.BASE_URL}/sports/{sport_key}/odds",
                        params={
                            "apiKey": self.api_key,
                            "regions": self.regions,
                            "markets": "h2h",
                            "oddsFormat": "decimal",
                        },
                        timeout=self.timeout,
                    )
                except requests.RequestException as exc:
                    # Avoid logging the request URL because this provider places its
                    # API key in a query parameter.
                    raise RuntimeError(f"Odds API network error ({type(exc).__name__})") from None
                if not response.ok:
                    raise RuntimeError(f"Odds API returned HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, list):
                    continue
                self._cache[sport_key] = (now, payload)
            for odds_event in payload:
                if not _same_match(names, odds_event, self.team_aliases):
                    continue
                odds_start = _timestamp(odds_event.get("commence_time"))
                if bayse_start is not None:
                    if odds_start is None or abs(odds_start - bayse_start) > 12 * 60 * 60:
                        continue
                elif odds_start is None:
                    continue
                matches.append(odds_event)
        unique = {str(item.get("id")): item for item in matches if item.get("id")}
        # Without a Bayse start time, more than one same-team fixture is ambiguous.
        if bayse_start is None and len(unique) > 1:
            return []
        return list(unique.values())

    def consensus_probabilities(self, bayse_event: dict) -> dict:
        """Return no-vig probabilities by participant, requiring fresh book quotes."""
        odds_events = self.get_event_odds(bayse_event)
        if not odds_events:
            return {"probabilities": {}, "bookmakers": 0, "stale": False}
        now = time.time()
        per_book = {}
        stale_only = True
        for event in odds_events:
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    updated = _timestamp(bookmaker.get("last_update") or market.get("last_update"))
                    if updated is None or now - updated > self.max_age_seconds or updated > now + 15:
                        continue
                    stale_only = False
                    raw = {}
                    for outcome in market.get("outcomes", []):
                        name = str(outcome.get("name", "")).strip()
                        try:
                            price = float(outcome.get("price"))
                        except (TypeError, ValueError):
                            continue
                        if name and price > 1:
                            raw[name] = 1.0 / price
                    total = sum(raw.values())
                    if total <= 0:
                        continue
                    book_key = str(bookmaker.get("key") or bookmaker.get("title") or "")
                    per_book[book_key] = {name: prob / total for name, prob in raw.items()}

        aggregate = {}
        participant_names = set()
        for values in per_book.values():
            participant_names.update(values)
        for name in participant_names:
            values = [book[name] for book in per_book.values() if name in book]
            if len(values) >= self.min_bookmakers:
                aggregate[name] = statistics.median(values)
        normalizer = sum(aggregate.values())
        if normalizer > 0:
            aggregate = {name: value / normalizer for name, value in aggregate.items()}
        return {
            "probabilities": aggregate,
            "bookmakers": len(per_book),
            "stale": stale_only and bool(odds_events),
        }


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _bayse_team_names(event: dict) -> list[str]:
    participants = event.get("participants") or event.get("competitors") or []
    names = []
    if isinstance(participants, list):
        for participant in participants:
            if isinstance(participant, dict):
                name = participant.get("name") or participant.get("title") or participant.get("label")
                if name:
                    names.append(str(name))
    title = str(event.get("title") or "")
    for delimiter in (" vs ", " v ", " versus ", " @ "):
        parts = re.split(delimiter, title, flags=re.IGNORECASE)
        if len(parts) == 2:
            names.extend(part.strip(" ?:-") for part in parts if part.strip(" ?:-"))
            break
    # Only retain names that are represented as distinct title fragments.
    unique = []
    for name in names:
        clean = name.strip()
        if clean and _norm(clean) not in {_norm(item) for item in unique}:
            unique.append(clean)
    return unique[:2]


def _bayse_start_time(event: dict) -> Optional[float]:
    for key in ("startDate", "eventDate", "commenceTime", "scheduledAt", "startsAt", "gameDate"):
        parsed = _timestamp(event.get(key))
        if parsed is not None:
            return parsed
    return None


def _canonical_team(name: str, aliases: dict) -> str:
    target = _norm(name)
    for canonical, variants in aliases.items():
        choices = variants if isinstance(variants, list) else [variants]
        if target == _norm(canonical) or target in {_norm(value) for value in choices}:
            return _norm(canonical)
    return target


def _same_match(team_names: Iterable[str], odds_event: dict, aliases: dict) -> bool:
    expected = {_canonical_team(name, aliases) for name in team_names if _norm(name)}
    actual = {
        _canonical_team(odds_event.get("home_team"), aliases),
        _canonical_team(odds_event.get("away_team"), aliases),
    }
    actual.discard("")
    return len(expected) == 2 and expected == actual


def _timestamp(value) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def outcome_probability(label: str, event: dict, probabilities: dict,
                        aliases: Optional[dict] = None) -> Optional[float]:
    """Resolve only exact participant or draw labels; never guess team aliases."""
    aliases = aliases or {}
    target = _canonical_team(label, aliases)
    for participant in (event.get("home_team"), event.get("away_team")):
        if participant and target == _canonical_team(participant, aliases):
            for name, probability in probabilities.items():
                if _canonical_team(name, aliases) == target:
                    return probability
    if target in {"draw", "tie"}:
        for name, probability in probabilities.items():
            if _norm(name) in {"draw", "tie"}:
                return probability
    return None
