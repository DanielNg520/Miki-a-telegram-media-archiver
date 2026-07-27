from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOKEN_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)


class Route(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    thread_id: int = Field(gt=0)
    keywords: list[str] = Field(default_factory=list)

    @field_validator("keywords", mode="after")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))

    @field_validator("name", mode="after")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("route name must not be blank")
        return normalized


@dataclass(frozen=True)
class RouteDecision:
    route_name: str
    thread_id: int
    reason: str


def extract_terms(text: str) -> set[str]:
    return {match.group(0).casefold() for match in TOKEN_RE.finditer(text)}


def route_candidates(text: str, routes: list[Route]) -> set[str]:
    configured_terms = {keyword for route in routes for keyword in route.keywords}
    return {
        keyword
        for keyword in configured_terms
        if re.search(
            rf"(?<![^\W_]){re.escape(keyword)}(?![^\W_])",
            text.casefold(),
            re.UNICODE,
        )
    }


def choose_route(matches: set[str], routes: list[Route]) -> RouteDecision | None:
    normalized_matches = {match.casefold() for match in matches}
    for route in routes:
        keyword = next((term for term in route.keywords if term in normalized_matches), None)
        if keyword is not None:
            return RouteDecision(route.name, route.thread_id, f"database:{keyword}")
    return None
