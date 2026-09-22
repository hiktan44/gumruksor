"""Sınıflandırma ölçüm koşucusu: etiketli vakaları gerçek hattan geçirir.

``customs_benchmark.py`` bugüne kadar **yalnız puanlıyordu**: kendisine verilen bir
tahmin dosyasını etiketlerle karşılaştırıyordu, ama o tahminleri üretecek bir koşucu
yoktu. Sonuç: hattın doğruluğu **hiç ölçülmemişti** ve her iyileştirme körlemesine
yapılıyordu. Bu modül eksik yarıyı kapatır: etiketli vakaları
``CustomsAdvisor.classify_product`` üzerinden geçirir, adayları kalıcı olarak kaydeder
ve mevcut puanlayıcıyla Top-1/Top-3 üretir.

Ölçümün sınırları — abartmamak için açıkça:

* Bu ölçüm **metin → GTİP** hattını ölçer, **görselden evsaf çıkarımını ölçmez**.
  Vakalar resmî kararların eşya tanımlarıdır; görsel katmanı ayrı bir ölçüm ister.
* ``classify_product`` tasarımı gereği **yalnız HS6/CN8** aday üretir
  (``VerifiedTariffCandidate.level``); 12 haneli GTİP üretmez, son haneler
  kullanıcının tarife ağacından seçtiği yaprakla belirlenir. Bu yüzden puanlayıcının
  ``top1_gtip12`` alanı bu katmanda **yapısal olarak 0**'dır ve "%0 doğruluk" diye
  okunmamalıdır; Türk vakalarında anlamlı ölçüt ``top1_cn8``/``top3_cn8``'dir
  (beklenen GTİP12'nin ilk 8 hanesi). Rapor bu notu her zaman taşır.
* Koşu model çağrısı içerdiği için **LLM kotası harcar** ve aynı vaka iki koşuda
  farklı sonuç verebilir; bu yüzden her tahmin kaydı hangi modellerle ve hangi
  commit'te üretildiğini taşır.

Kullanım::

    uv run python classification_benchmark.py --dry-run          # yalnız vakaları listeler
    uv run python classification_benchmark.py --score-only       # kayıtlı tahminleri puanlar
    uv run python classification_benchmark.py --limit 4          # 4 vakayı gerçek hattan geçirir
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import customs_benchmark

ROOT = Path(__file__).resolve().parent
CASE_DIR = ROOT / "benchmarks"

#: Etiketli veri setleri. Anahtar CLI ve yönetim rotasındaki ``dataset`` değeridir.
DATASETS: dict[str, str] = {
    "eu": "customs_classification_v1.jsonl",
    "tr": "turkish_btb_gtip12_historical_v1.jsonl",
}

#: Aynı anda kaç vaka çalışsın. Sağlayıcı hız sınırına takılmamak için düşük tutulur;
#: ``classification`` rotası canlıda dakikada 20 istekle sınırlı.
DEFAULT_CONCURRENCY = max(1, min(int(os.environ.get("BENCHMARK_CONCURRENCY") or 3), 6))

#: Vaka başına süre tavanı. Aşan vaka **yanlış değil, ölçülemedi** sayılır.
DEFAULT_CASE_TIMEOUT_SECONDS = max(10.0, float(os.environ.get("BENCHMARK_CASE_TIMEOUT") or 120.0))

GTIP12_NOTE = (
    "classify_product yalnız HS6/CN8 aday üretir; 12 haneli GTİP tarife ağacından seçilir. "
    "Bu yüzden top1_gtip12/top3_gtip12 bu katmanda yapısal olarak 0'dır ve doğruluk ölçüsü "
    "değildir. Türk vakalarında ölçüt top1_cn8/top3_cn8'dir."
)

WARNING = (
    "Bu ölçüm metinden GTİP adayı üretme başarımını gösterir; görselden evsaf çıkarımını, "
    "güncel tarife geçerliliğini veya hukuki bağlayıcılığı ölçmez."
)


class ClassifierLike(Protocol):
    """Koşucunun ihtiyaç duyduğu tek yüzey; testte sahte nesne geçilir."""

    async def classify_product(self, request: Any) -> Any:  # pragma: no cover - protokol
        ...


# ------------------------------------------------------------------ vakalar
def load_cases(dataset: str = "all", *, case_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Etiketli vakaları yükler ve her birine ``dataset`` etiketi ekler.

    Doğrulama ``customs_benchmark.load_cases``'e bırakılır: resmî olmayan kaynak,
    eksik sha256 veya hedef hanesi bozuk satır orada reddedilir.
    """
    directory = Path(case_dir or CASE_DIR)
    names = sorted(DATASETS) if dataset in {"all", ""} else [dataset]
    unknown = [name for name in names if name not in DATASETS]
    if unknown:
        raise ValueError(f"Bilinmeyen veri seti: {', '.join(unknown)}")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        for case in customs_benchmark.load_cases(directory / DATASETS[name]):
            case_id = str(case["id"])
            if case_id in seen:
                raise ValueError(f"Veri setlerinde yinelenen vaka kimliği: {case_id}")
            seen.add(case_id)
            case["dataset"] = name
            cases.append(case)
    return cases


def select_cases(
    cases: Sequence[dict[str, Any]],
    *,
    limit: int | None = None,
    offset: int = 0,
    skip_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Parti seçer. ``skip_ids`` zaten kayıtlı vakaları atlamak için."""
    skip = {str(item) for item in skip_ids}
    pending = [case for case in cases if str(case["id"]) not in skip]
    pending = pending[max(0, int(offset)) :]
    if limit is not None and limit >= 0:
        pending = pending[: int(limit)]
    return pending


def request_from_case(case: dict[str, Any]) -> Any:
    """Vakayı sınıflandırma isteğine çevirir.

    Yalnız **eşya tanımı** verilir: resmî kararın kendi metni. Ek alan uydurmak
    ölçümü kolaylaştırır ama gerçek kullanımı temsil etmez.
    """
    from customs_advisor import ProductClassificationRequest

    return ProductClassificationRequest(product_description=str(case["description"]))


# ------------------------------------------------------------------ koşu
@dataclass(slots=True)
class CasePrediction:
    """Tek vakanın hat çıktısı. ``candidates`` puanlayıcının beklediği sırada."""

    id: str
    dataset: str
    candidates: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    confidence_scores: list[int] = field(default_factory=list)
    status: str = ""
    verification_status: str = ""
    models: list[str] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int = 0
    code_version: str = ""
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "candidates": list(self.candidates),
            "levels": list(self.levels),
            "confidence_scores": list(self.confidence_scores),
            "status": self.status,
            "verification_status": self.verification_status,
            "models": list(self.models),
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "code_version": self.code_version,
            "recorded_at": self.recorded_at,
        }


def _code_version() -> str:
    return (os.environ.get("SOURCE_COMMIT") or "")[:12]


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def run_case(
    service: ClassifierLike,
    case: dict[str, Any],
    *,
    timeout: float = DEFAULT_CASE_TIMEOUT_SECONDS,
) -> CasePrediction:
    """Tek vakayı çalıştırır; hata **vaka bazında** kaydedilir, koşu durmaz."""
    prediction = CasePrediction(
        id=str(case["id"]),
        dataset=str(case.get("dataset") or ""),
        code_version=_code_version(),
        recorded_at=_now(),
    )
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            service.classify_product(request_from_case(case)), timeout=timeout
        )
    except (TimeoutError, asyncio.TimeoutError):
        prediction.error = f"timeout: {timeout:.0f} sn"
    except Exception as exc:  # hat hatası ölçümü bozmaz, kaydedilir
        prediction.error = f"{type(exc).__name__}: {str(exc)[:220]}"
    else:
        prediction.status = str(getattr(result, "status", "") or "")
        prediction.verification_status = str(getattr(result, "verification_status", "") or "")
        prediction.models = [str(item) for item in (getattr(result, "models", None) or [])]
        for candidate in getattr(result, "candidates", None) or []:
            prediction.candidates.append(str(getattr(candidate, "code", "") or ""))
            prediction.levels.append(str(getattr(candidate, "level", "") or ""))
            prediction.confidence_scores.append(int(getattr(candidate, "confidence_score", 0) or 0))
    prediction.elapsed_ms = int((time.monotonic() - started) * 1000)
    return prediction


async def run_cases(
    service: ClassifierLike,
    cases: Sequence[dict[str, Any]],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_CASE_TIMEOUT_SECONDS,
) -> list[CasePrediction]:
    """Vakaları sınırlı eşzamanlılıkla çalıştırır ve giriş sırasını korur."""
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def guarded(case: dict[str, Any]) -> CasePrediction:
        async with semaphore:
            return await run_case(service, case, timeout=timeout)

    if not cases:
        return []
    return list(await asyncio.gather(*(guarded(case) for case in cases)))


# ------------------------------------------------------------------ puanlama
def _breakdown(cases: Sequence[dict[str, Any]], predictions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(item.get("id")): item for item in predictions}
    levels: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    measured = 0
    for case in cases:
        prediction = by_id.get(str(case["id"]))
        if prediction is None:
            continue
        if prediction.get("error"):
            errors.append({"id": str(case["id"]), "error": str(prediction["error"])})
            continue
        measured += 1
        for level in prediction.get("levels") or []:
            levels[str(level)] = levels.get(str(level), 0) + 1
    return {"measured_cases": measured, "candidate_levels": levels, "errors": errors}


def evaluate(cases: Sequence[dict[str, Any]], predictions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Puanlar ve veri seti bazında ayırır.

    Puanlama ``customs_benchmark.evaluate_predictions``'a bırakılır: tek bir doğruluk
    tanımı olsun, koşucu kendi ölçütünü uydurmasın.
    """
    case_list = list(cases)
    prediction_list = [dict(item) for item in predictions]
    report = customs_benchmark.evaluate_predictions(case_list, prediction_list)
    report.update(_breakdown(case_list, prediction_list))
    report["gtip12_note"] = GTIP12_NOTE
    report["runner_warning"] = WARNING
    report["missing_predictions"] = sorted(
        str(case["id"]) for case in case_list if str(case["id"]) not in {str(item.get("id")) for item in prediction_list}
    )
    report["code_versions"] = sorted(
        {str(item.get("code_version") or "") for item in prediction_list if item.get("code_version")}
    )
    by_dataset: dict[str, Any] = {}
    for name in sorted({str(case.get("dataset") or "") for case in case_list}):
        if not name:
            continue
        subset = [case for case in case_list if str(case.get("dataset") or "") == name]
        subset_ids = {str(case["id"]) for case in subset}
        subset_predictions = [item for item in prediction_list if str(item.get("id")) in subset_ids]
        nested = customs_benchmark.evaluate_predictions(subset, subset_predictions)
        nested.update(_breakdown(subset, subset_predictions))
        nested.pop("details", None)
        by_dataset[name] = nested
    report["by_dataset"] = by_dataset
    return report


# ------------------------------------------------------------------ kalıcılık
class BenchmarkStore:
    """Tahminleri kalıcı tutar; parti parti koşulan ölçüm tek skora toplanabilsin.

    Canlıda 16 vaka tek HTTP isteğinde bitmez (her vaka iki model çağrısı), bu yüzden
    yönetim rotası küçük partiler koşar. Toplam skor ancak tahminler bir yerde
    birikiyorsa anlamlı olur.
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "classification_benchmark.sqlite3"
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    id TEXT PRIMARY KEY,
                    dataset TEXT NOT NULL DEFAULT '',
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    levels_json TEXT NOT NULL DEFAULT '[]',
                    confidence_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT '',
                    models_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    code_version TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def save(self, predictions: Iterable[CasePrediction]) -> int:
        rows = [
            (
                item.id,
                item.dataset,
                json.dumps(item.candidates, ensure_ascii=False),
                json.dumps(item.levels, ensure_ascii=False),
                json.dumps(item.confidence_scores),
                item.status,
                item.verification_status,
                json.dumps(item.models, ensure_ascii=False),
                item.error,
                int(item.elapsed_ms),
                item.code_version,
                item.recorded_at,
            )
            for item in predictions
        ]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO predictions(
                    id, dataset, candidates_json, levels_json, confidence_json, status,
                    verification_status, models_json, error, elapsed_ms, code_version, recorded_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    dataset=excluded.dataset,
                    candidates_json=excluded.candidates_json,
                    levels_json=excluded.levels_json,
                    confidence_json=excluded.confidence_json,
                    status=excluded.status,
                    verification_status=excluded.verification_status,
                    models_json=excluded.models_json,
                    error=excluded.error,
                    elapsed_ms=excluded.elapsed_ms,
                    code_version=excluded.code_version,
                    recorded_at=excluded.recorded_at
                """,
                rows,
            )
        return len(rows)

    def load(self, dataset: str = "all") -> list[dict[str, Any]]:
        query = "SELECT * FROM predictions"
        params: tuple[Any, ...] = ()
        if dataset not in {"all", ""}:
            query += " WHERE dataset=?"
            params = (dataset,)
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY id", params).fetchall()
        return [
            {
                "id": row["id"],
                "dataset": row["dataset"],
                "candidates": json.loads(row["candidates_json"]),
                "levels": json.loads(row["levels_json"]),
                "confidence_scores": json.loads(row["confidence_json"]),
                "status": row["status"],
                "verification_status": row["verification_status"],
                "models": json.loads(row["models_json"]),
                "error": row["error"],
                "elapsed_ms": row["elapsed_ms"],
                "code_version": row["code_version"],
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def stored_ids(self, dataset: str = "all") -> set[str]:
        """Hatalı kaydedilen vaka **kayıtlı sayılmaz**: sonraki turda yeniden denenir."""
        return {str(item["id"]) for item in self.load(dataset) if not item.get("error")}

    def clear(self, dataset: str = "all") -> int:
        with self._connect() as connection:
            if dataset in {"all", ""}:
                cursor = connection.execute("DELETE FROM predictions")
            else:
                cursor = connection.execute("DELETE FROM predictions WHERE dataset=?", (dataset,))
        return int(cursor.rowcount or 0)


async def run_and_score(
    service: ClassifierLike,
    *,
    dataset: str = "all",
    limit: int | None = None,
    offset: int = 0,
    resume: bool = True,
    store: BenchmarkStore | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_CASE_TIMEOUT_SECONDS,
    case_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Bir parti koşar, kaydeder ve **kayıtlı tüm** tahminleri puanlar."""
    cases = load_cases(dataset, case_dir=case_dir)
    ledger = store if store is not None else BenchmarkStore()
    skip = ledger.stored_ids(dataset) if resume else set()
    batch = select_cases(cases, limit=limit, offset=offset, skip_ids=skip)
    predictions = await run_cases(service, batch, concurrency=concurrency, timeout=timeout)
    ledger.save(predictions)
    report = evaluate(cases, ledger.load(dataset))
    report["batch"] = {
        "requested": len(batch),
        "case_ids": [str(case["id"]) for case in batch],
        "failed": [item.to_dict() for item in predictions if item.error],
    }
    # **Kalan vaka listesi burada da dönmeli.** Canlıda ölçtüğüm hata: bu alanı yalnız
    # ``GET`` rotası eklediği için parti koşusundan sonra panel her seferinde "Tüm
    # vakalar ölçüldü" yazıyordu — 8/16'da bile. Kullanıcı bu yüzden ölçümü yarıda
    # bıraktı ve yarım ölçümü tam sanıp okudu. Alan eksik olduğunda arayüzün
    # varsayılanı "bitti" oluyor, yani sessiz değil, **yanlış** cevap veriyor.
    report["pending_cases"] = [
        str(case["id"])
        for case in select_cases(cases, skip_ids=ledger.stored_ids(dataset))
    ]
    return report


# ------------------------------------------------------------------ CLI
def _build_service() -> ClassifierLike:
    """Gerçek hattı kurar. Tarife motoru zorunlu; anahtar yoksa koşu hata verir."""
    from customs_advisor import CustomsAdvisor
    from tariff_engine import TariffEngine

    return CustomsAdvisor(tariff_engine=TariffEngine())


def main() -> None:
    parser = argparse.ArgumentParser(description="Sınıflandırma doğruluk ölçümü")
    parser.add_argument("--dataset", default="all", choices=[*sorted(DATASETS), "all"])
    parser.add_argument("--limit", type=int, default=None, help="Bu turda kaç vaka koşulacak")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=float, default=DEFAULT_CASE_TIMEOUT_SECONDS)
    parser.add_argument("--no-resume", action="store_true", help="Kayıtlı vakaları da yeniden koş")
    parser.add_argument("--score-only", action="store_true", help="Model çağırmadan kayıtlı tahminleri puanla")
    parser.add_argument("--dry-run", action="store_true", help="Yalnız koşulacak vakaları listele")
    parser.add_argument("--clear", action="store_true", help="Kayıtlı tahminleri sil")
    parser.add_argument("--out", default=None, help="Raporu bu dosyaya da yaz")
    args = parser.parse_args()

    store = BenchmarkStore()
    if args.clear:
        print(json.dumps({"cleared": store.clear(args.dataset)}, ensure_ascii=False))
        return
    cases = load_cases(args.dataset)
    if args.dry_run:
        skip = set() if args.no_resume else store.stored_ids(args.dataset)
        batch = select_cases(cases, limit=args.limit, offset=args.offset, skip_ids=skip)
        report: dict[str, Any] = {
            "case_count": len(cases),
            "already_stored": len(skip),
            "would_run": [case["id"] for case in batch],
            "runner_warning": WARNING,
        }
    elif args.score_only:
        report = evaluate(cases, store.load(args.dataset))
    else:
        report = asyncio.run(
            run_and_score(
                _build_service(),
                dataset=args.dataset,
                limit=args.limit,
                offset=args.offset,
                resume=not args.no_resume,
                store=store,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
