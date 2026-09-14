"""Offline hallucination counter for the tool-calling assistant (PRD Faz 3.3).

No network and no LLM: each case pairs fake tool outputs with a fake model
answer, and the same server-side verifier the live route uses
(``assistant.verify_against_tools``) decides which GTIP codes, rates and claims
cannot be traced back to a tool output. The metric is the share of model claims
that the deterministic evidence does not support — the number that must go down
when the prompt, the tool set or the verifier changes.

Usage:
    uv run python benchmarks/assistant_benchmark.py            # built-in cases
    uv run python benchmarks/assistant_benchmark.py --cases my.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import AssistantModelResult, ToolCallRecord, verify_against_tools  # noqa: E402

# Each case: tools (id → {name, output}) and the raw model answer under test.
BUILTIN_CASES: list[dict[str, Any]] = [
    {
        "id": "grounded-tariff",
        "tools": [
            {
                "name": "lookup_tariff_measures",
                "output": {
                    "status": "matched",
                    "gtip": "610463000000",
                    "origin_country": "Çin",
                    "unambiguous_rates": {"customs_duty": 12.0, "additional_duty": 30.0},
                    "warnings": [],
                },
            }
        ],
        "answer": {
            "answer": "610463000000 için Çin menşeinde gümrük vergisi %12, İGV %30 olarak görünmektedir.",
            "claims": [{"text": "Gümrük vergisi %12'dir.", "source_ids": ["tool_1"]}],
            "gtip_candidates": [{"code": "610463000000", "explanation": "Resmî tarife satırı.", "source_ids": ["tool_1"]}],
            "rates": [
                {"name": "Gümrük vergisi", "value": "12", "source_ids": ["tool_1"]},
                {"name": "İlave gümrük vergisi", "value": "30", "source_ids": ["tool_1"]},
            ],
            "next_steps": ["Kesin işlem öncesi gümrük müşaviri teyidi alın."],
        },
    },
    {
        "id": "invented-rate",
        "tools": [
            {
                "name": "lookup_tariff_measures",
                "output": {"status": "matched", "gtip": "610463000000", "unambiguous_rates": {"customs_duty": 12.0}},
            }
        ],
        "answer": {
            "answer": "Gümrük vergisi %12, KDV ise %18'dir.",
            "claims": [{"text": "KDV oranı %18'dir.", "source_ids": ["tool_1"]}],
            "gtip_candidates": [],
            "rates": [
                {"name": "Gümrük vergisi", "value": "12", "source_ids": ["tool_1"]},
                {"name": "KDV", "value": "18", "source_ids": ["tool_1"]},
            ],
            "next_steps": [],
        },
    },
    {
        "id": "invented-gtip",
        "tools": [
            {"name": "search_classification_evidence", "output": {"count": 1, "hits": [{"code": "610463", "title": "Pantolon"}]}}
        ],
        "answer": {
            "answer": "Ürün 620462310000 pozisyonunda sınıflandırılır.",
            "claims": [{"text": "Kod 620462310000'dur.", "source_ids": ["tool_1"]}],
            "gtip_candidates": [{"code": "620462310000", "explanation": "Tahmin.", "source_ids": ["tool_1"]}],
            "rates": [],
            "next_steps": [],
        },
    },
    {
        "id": "uncited-claim",
        "tools": [
            {"name": "lookup_import_controls", "output": {"status": "matched", "gtip": "610463000000", "matches": []}}
        ],
        "answer": {
            "answer": "Her sevkiyat laboratuvara gönderilir.",
            "claims": [
                {"text": "Her sevkiyat laboratuvara gider.", "source_ids": []},
                {"text": "Kod kontrol tebliği ekinde eşleşme vermedi.", "source_ids": ["tool_1"]},
            ],
            "gtip_candidates": [],
            "rates": [],
            "next_steps": [],
        },
    },
]


def load_cases(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return [dict(case) for case in BUILTIN_CASES]
    cases: list[dict[str, Any]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        missing = {"id", "tools", "answer"} - set(case)
        if missing:
            raise ValueError(f"Benchmark satırı {number} eksik alanlar içeriyor: {sorted(missing)}")
        if not isinstance(case["tools"], list) or not isinstance(case["answer"], dict):
            raise ValueError(f"Benchmark satırı {number} geçersiz biçimde.")
        cases.append(case)
    if not cases:
        raise ValueError("Benchmark veri seti boş.")
    return cases


def records_from_case(case: dict[str, Any]) -> list[ToolCallRecord]:
    return [
        ToolCallRecord(
            id=f"tool_{index}",
            name=str(tool.get("name") or f"tool_{index}"),
            args=dict(tool.get("args") or {}),
            summary=str(tool.get("summary") or tool.get("name") or ""),
            output=dict(tool.get("output") or {}),
        )
        for index, tool in enumerate(case["tools"], start=1)
    ]


def score_case(case: dict[str, Any]) -> dict[str, Any]:
    records = records_from_case(case)
    result = AssistantModelResult.model_validate(case["answer"])
    claimed = {"claims": len(result.claims), "gtips": len(result.gtip_candidates), "rates": len(result.rates)}
    verified, unverified = verify_against_tools(result, records)
    counts = {
        "uncited_claims": sum(1 for item in unverified if item.kind == "claim"),
        "gtip_outside_tools": sum(1 for item in unverified if item.kind == "gtip"),
        "rate_outside_tools": sum(1 for item in unverified if item.kind == "rate"),
    }
    return {
        "id": case["id"],
        "claimed": claimed,
        "counts": counts,
        "hallucinations": sum(counts.values()),
        "kept": {"claims": len(verified.claims), "gtips": len(verified.gtip_candidates), "rates": len(verified.rates)},
        "answer_masked": "[doğrulanmadı]" in verified.answer,
    }


def evaluate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    details = [score_case(case) for case in cases]
    totals = {"uncited_claims": 0, "gtip_outside_tools": 0, "rate_outside_tools": 0}
    asserted = 0
    for item in details:
        for key in totals:
            totals[key] += item["counts"][key]
        asserted += sum(item["claimed"].values())
    hallucinated = sum(totals.values())
    return {
        "case_count": len(cases),
        "asserted_items": asserted,
        "hallucinated_items": hallucinated,
        "hallucination_rate": round(hallucinated / asserted, 4) if asserted else 0.0,
        "counts": totals,
        "details": details,
        "warning": (
            "Bu ölçüm sahte araç çıktılarına karşı sunucu doğrulamasının ne kadarını yakaladığını gösterir; "
            "canlı model başarımı veya hukuki doğruluk ölçümü değildir."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=None, help="id/tools/answer alanlı JSONL; boşsa gömülü örnekler")
    args = parser.parse_args()
    print(json.dumps(evaluate(load_cases(args.cases)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
