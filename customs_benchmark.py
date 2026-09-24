"""Reproducible metrics for the source-anchored customs classification benchmark."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_OFFICIAL_HOSTS = {
    "taxation-customs.ec.europa.eu",
    "eur-lex.europa.eu",
    "ticaret.gov.tr",
    "www.ticaret.gov.tr",
    "ggm.ticaret.gov.tr",
    "istanbulbolge.ticaret.gov.tr",
}


def _code(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        required = {"id", "description", "source_page", "source_url", "source_sha256"}
        missing = required - set(case)
        if missing:
            raise ValueError(f"Benchmark satırı {line_number} eksik alanlar içeriyor: {sorted(missing)}")
        if (urlsplit(case["source_url"]).hostname or "").lower() not in _OFFICIAL_HOSTS:
            raise ValueError(f"Benchmark satırı {line_number} resmî olmayan kaynak içeriyor.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(case["source_sha256"])):
            raise ValueError(f"Benchmark satırı {line_number} geçersiz SHA-256 içeriyor.")
        case["expected_gtip12"] = [_code(item) for item in case.get("expected_gtip12", [])]
        if case["expected_gtip12"] and not all(len(item) == 12 for item in case["expected_gtip12"]):
            raise ValueError(f"Benchmark satırı {line_number} GTİP12 dışı hedef içeriyor.")
        case["expected_cn8"] = [
            _code(item) for item in case.get("expected_cn8", [])
        ] or sorted({item[:8] for item in case["expected_gtip12"]})
        case["accepted_hs6"] = [
            _code(item) for item in case.get("accepted_hs6", [])
        ] or sorted({item[:6] for item in case["expected_gtip12"]})
        if not case["expected_cn8"] or not case["accepted_hs6"]:
            raise ValueError(
                f"Benchmark satırı {line_number} CN8/HS6 veya türetilebilir GTİP12 hedefi içermiyor."
            )
        if not all(len(item) == 8 for item in case["expected_cn8"]):
            raise ValueError(f"Benchmark satırı {line_number} CN8 dışı hedef içeriyor.")
        if not all(len(item) == 6 for item in case["accepted_hs6"]):
            raise ValueError(f"Benchmark satırı {line_number} HS6 dışı hedef içeriyor.")
        if case["expected_gtip12"]:
            if case.get("jurisdiction") != "TR" or not isinstance(case.get("tariff_year"), int):
                raise ValueError(
                    f"Benchmark satırı {line_number} Türk GTİP12 hedefi için jurisdiction=TR ve tariff_year içermelidir."
                )
        cases.append(case)
    if not cases:
        raise ValueError("Benchmark veri seti boş.")
    return cases


def evaluate_predictions(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(item.get("id")): item for item in predictions}
    totals = {
        "top1_hs6": 0,
        "top3_hs6": 0,
        "top1_cn8": 0,
        "top3_cn8": 0,
        "top1_gtip12": 0,
        "top3_gtip12": 0,
        "abstained": 0,
        # Hiç koşulmamış vaka **çekimser değildir**: model konuşmadı çünkü ona hiç
        # sorulmadı. İkisini aynı sayaçta toplamak canlıda yanlış rapora yol açtı —
        # 8/16 koşulmuş bir ölçümde panel "çekimser %50" diyordu, oysa o 8 vaka
        # koşulmamıştı ve koşulanların 8'i de doğruydu. İki ayrı sayaç, iki ayrı gerçek.
        "not_run": 0,
        # **CN8 ölçütünün iki ayrı şeyi topladığı yer.** ``top1_cn8`` ilk adayın en az 8
        # haneli OLMASINI şart koşar; model 6 hanede durursa kod doğru olsa bile
        # başarısız yazılır. Canlı ölçümde 6 CN8 hatasının 6'sı buydu ve rakam "8 hane
        # doğruluğu" sanıldı. Bu sayaç ikisini ayırır: kaç vakada 8 haneye inmeye
        # çalıştı (aşağıda ``cn8_attempted``), indiğinde ne kadar doğruydu.
        "cn8_attempted": 0,
    }
    cn8_attempted_correct = 0
    gtip12_case_count = 0
    details: list[dict[str, Any]] = []
    for case in cases:
        prediction = by_id.get(case["id"])
        ran = prediction is not None
        prediction = prediction or {}
        codes = [_code(item) for item in prediction.get("candidates", []) if _code(item)][:3]
        expected_cn8 = set(case["expected_cn8"])
        accepted_hs6 = set(case["accepted_hs6"])
        expected_gtip12 = set(case.get("expected_gtip12", []))
        top1_hs6 = bool(codes and codes[0][:6] in accepted_hs6)
        top3_hs6 = any(code[:6] in accepted_hs6 for code in codes)
        top1_cn8 = bool(codes and len(codes[0]) >= 8 and codes[0][:8] in expected_cn8)
        top3_cn8 = any(len(code) >= 8 and code[:8] in expected_cn8 for code in codes)
        top1_gtip12 = bool(codes and len(codes[0]) == 12 and codes[0] in expected_gtip12)
        top3_gtip12 = any(len(code) == 12 and code in expected_gtip12 for code in codes)
        totals["top1_hs6"] += int(top1_hs6)
        totals["top3_hs6"] += int(top3_hs6)
        totals["top1_cn8"] += int(top1_cn8)
        totals["top3_cn8"] += int(top3_cn8)
        totals["top1_gtip12"] += int(top1_gtip12)
        totals["top3_gtip12"] += int(top3_gtip12)
        totals["abstained"] += int(ran and not codes)
        totals["not_run"] += int(not ran)
        cn8_attempted = bool(codes and len(codes[0]) >= 8)
        totals["cn8_attempted"] += int(cn8_attempted)
        cn8_attempted_correct += int(cn8_attempted and top1_cn8)
        gtip12_case_count += int(bool(expected_gtip12))
        details.append(
            {
                "id": case["id"],
                "candidates": codes,
                "cn8_attempted": cn8_attempted,
                "top1_hs6": top1_hs6,
                "top3_hs6": top3_hs6,
                "top1_cn8": top1_cn8,
                "top3_cn8": top3_cn8,
                "top1_gtip12": top1_gtip12 if expected_gtip12 else None,
                "top3_gtip12": top3_gtip12 if expected_gtip12 else None,
            }
        )
    count = len(cases)
    metrics = {
        key: round(value / count, 4)
        for key, value in totals.items()
        if key not in {"top1_gtip12", "top3_gtip12"}
    }
    metrics["top1_gtip12"] = (
        round(totals["top1_gtip12"] / gtip12_case_count, 4) if gtip12_case_count else None
    )
    metrics["top3_gtip12"] = (
        round(totals["top3_gtip12"] / gtip12_case_count, 4) if gtip12_case_count else None
    )
    if gtip12_case_count == 0:
        warning = "Bu ölçüm AB CN8 karar örnekleridir; Türk GTİP12 doğruluğu veya hukuki bağlayıcılık ölçümü değildir."
    else:
        warning = (
            "Türk GTİP12 ölçümü tarihsel ve yayımlanmış BTB örneklerine dayanır. Kodların güncel tarife "
            "geçerliliğini veya başka kişiler bakımından hukuki bağlayıcılığını göstermez."
        )
    # **İki ayrı soru, iki ayrı oran.** ``metrics`` bütün vakalara böler: "bu veri
    # setinin tamamında ne kadar doğru" sorusunun cevabı ve kısmi ölçümde kasıtlı
    # olarak düşüktür (koşulmayan vaka başarısız sayılır). ``metrics_measured``
    # yalnız koşulan vakalara böler: "sistem cevap verdiğinde ne kadar doğru".
    # Birini diğerinin yerine koymak raporu yanıltıcı yapar, bu yüzden ikisi de döner.
    measured = count - totals["not_run"]
    metrics_measured = (
        {
            key: round(value / measured, 4)
            for key, value in totals.items()
            if key not in {"top1_gtip12", "top3_gtip12", "not_run"}
        }
        if measured
        else {}
    )
    # **Üçüncü oran: "8 haneye indiğinde ne kadar doğru".** ``metrics["top1_cn8"]``
    # bu soruyu cevaplamaz, çünkü 6 hanede kalan vakayı da başarısız sayar. Bu iki alan
    # ölçütü ikiye böler: ``cn8_attempted`` çabayı, ``top1_cn8_when_attempted`` isabeti
    # ölçer. İstem değişikliğinin işe yarayıp yaramadığı yalnız bu ikisiyle görülür.
    attempted = totals["cn8_attempted"]
    return {
        "case_count": count,
        "gtip12_case_count": gtip12_case_count,
        "measured_case_count": measured,
        "cn8_attempted_case_count": attempted,
        "top1_cn8_when_attempted": (
            round(cn8_attempted_correct / attempted, 4) if attempted else None
        ),
        "metrics": metrics,
        "metrics_measured": metrics_measured,
        "counts": totals,
        "details": details,
        "warning": warning,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("predictions", help="id ve candidates alanlarını içeren JSON dosyası")
    parser.add_argument(
        "--cases",
        default=str(Path(__file__).with_name("benchmarks") / "customs_classification_v1.jsonl"),
    )
    args = parser.parse_args()
    cases = load_cases(args.cases)
    predictions = json.loads(Path(args.predictions).read_text(encoding="utf-8"))
    print(json.dumps(evaluate_predictions(cases, predictions), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
