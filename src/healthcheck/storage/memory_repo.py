"""메모리 저장소. 테스트와 fake 모드에서 쓴다."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

from healthcheck.domain.labels import Label
from healthcheck.ports.repository import CheckRecord, Metrics


class InMemoryCheckRepository:
    def __init__(self) -> None:
        self.records: list[CheckRecord] = []

    async def save(self, record: CheckRecord) -> None:
        self.records.append(record)

    async def metrics(self) -> Metrics:
        by_label: dict[str, int] = {}
        types: set[tuple[str, str]] = set()
        users: set[str] = set()
        versions: dict[str, int] = {}

        by_kind: dict[str, int] = {}
        daily: dict[str, int] = {}
        ingredients: dict[str, int] = {}

        for record in self.records:
            by_kind[record.input_kind] = by_kind.get(record.input_kind, 0) + 1
            day = record.created_at.date().isoformat()
            daily[day] = daily.get(day, 0) + 1
            if record.ingredient:
                ingredients[record.ingredient] = ingredients.get(record.ingredient, 0) + 1
            by_label[record.label.value] = by_label.get(record.label.value, 0) + 1
            versions[record.rules_version] = versions.get(record.rules_version, 0) + 1
            if record.label in (Label.WARNING, Label.CAUTION) and record.ingredient:
                types.add((record.rule_id, record.ingredient))
            if record.user_key:
                users.add(record.user_key)

        return Metrics(
            total_checks=len(self.records),
            by_label=by_label,
            misinformation_types=len(types),
            cache_hits=sum(1 for record in self.records if record.from_cache),
            distinct_users=len(users),
            rules_versions=versions,
            by_input_kind=by_kind,
            daily=dict(sorted(daily.items())),
            top_ingredients=sorted(ingredients.items(), key=lambda item: (-item[1], item[0]))[:10],
        )

    async def purge_expired(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        texts = 0
        hashed = 0

        for index, record in enumerate(self.records):
            changes: dict = {}
            if record.text_expires_at is not None and record.text_expires_at <= now:
                changes.update(raw_text=None, transcript_text=None, text_expires_at=None)
                texts += 1
            if (
                record.user_key
                and record.user_key_expires_at is not None
                and record.user_key_expires_at <= now
            ):
                changes.update(
                    user_key=hashlib.sha256(record.user_key.encode("utf-8")).hexdigest()[:32],
                    user_key_expires_at=None,
                )
                hashed += 1
            if changes:
                self.records[index] = replace(record, **changes)

        return {"texts_cleared": texts, "user_keys_hashed": hashed}
