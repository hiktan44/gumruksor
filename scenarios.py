"""Origin scenario rows: one deterministic tariff lookup per origin country.

The loop used to live inside the ``/api/tariff/scenarios`` route.  It is kept
here so the savings ranking (``savings.py``) can reuse exactly the same rows
without going through HTTP.  Output shape is unchanged.
"""
from __future__ import annotations

from typing import Any, Iterable

from origin_documents import origin_document_requirements


async def build_origin_scenarios(
    engine: Any,
    gtip: str,
    origins: Iterable[str],
    *,
    dispatch_country: str | None,
    atr_certificate: bool | None,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Look up the same tariff line for every origin and attach the document rule.

    ``as_of`` is accepted for forward compatibility with the date-based query
    (PRD 2.x); the tariff engine does not take it yet, so it is not forwarded.
    """
    rows: list[dict[str, Any]] = []
    for origin in origins:
        lookup = await engine.lookup(
            gtip, origin_country=origin, dispatch_country=dispatch_country, atr_certificate=atr_certificate
        )
        documents = origin_document_requirements(origin, gtip=lookup.gtip, dispatch_country=dispatch_country)
        rows.append(
            {
                "origin_country": origin,
                "dispatch_country": dispatch_country,
                "status": lookup.status,
                "origin_recognised": lookup.origin_recognised,
                "resolved_country_group": lookup.resolved_country_group,
                "matched_gtip_count": lookup.matched_gtip_count,
                "unambiguous_rates": lookup.unambiguous_rates or {},
                "ambiguous_measure_types": lookup.ambiguous_measure_types,
                "atr_free_circulation": lookup.atr_free_circulation,
                "atr_available": lookup.atr_available,
                "origin_proof_required": lookup.origin_proof_required,
                "fallback_rates": lookup.fallback_rates,
                "origin_documents": documents.model_dump(mode="json") if documents else None,
                "warnings": lookup.warnings,
            }
        )
    return rows
