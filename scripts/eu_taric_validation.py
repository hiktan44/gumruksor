#!/usr/bin/env python
"""AB TARIC doğrulaması: 10 kod için motorun çözümlediği özeti resmî ekran bağlantısıyla yan yana yazar.

Kullanıcının istediği kabul adımı budur: toplu entegrasyona geçmeden önce Apify aktöründen
gelen sonuçlar resmî TARIC ekranıyla karşılaştırılmalıdır. Komisyon, TARIC danışma
sayfalarını robots politikasıyla otomatik erişime kapattığı için karşılaştırma **elle**
yapılır; bu betik her kod için tarayıcıda açılacak resmî bağlantıyı da üretir.

    APIFY_TOKEN=... EU_TARIC_ENABLED=1 uv run python scripts/eu_taric_validation.py

Varsayılan kod listesi tekstil, elektronik, makine, tarım ve çelik gibi farklı davranan
fasıllardan seçilmiştir (gümrük birliği kapsamı dışında kalanlar dâhil).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eu_taric import EuTaricEngine  # noqa: E402

DEFAULT_CODES: tuple[str, ...] = (
    "6109100010",  # pamuklu tişört (tekstil, gümrük birliği)
    "8517130000",  # akıllı telefon (elektronik, sıfır oran beklenir)
    "8471300000",  # taşınabilir bilgi işlem makinesi
    "7306400090",  # paslanmaz çelik boru (AKÇT/damping ihtimali)
    "3920202190",  # polipropilen film (plastik)
    "8708299000",  # motorlu taşıt aksamı
    "0805102200",  # portakal (tarım — gümrük birliği dışı)
    "1701991000",  # şeker (tarım, tarım bileşeni ihtimali)
    "6403990000",  # ayakkabı
    "9403300000",  # büro mobilyası
)


def official_link(code: str, partner: str, day: str) -> str:
    return (
        "https://ec.europa.eu/taxation_customs/dds2/taric/measures.jsp"
        f"?Lang=en&SimDate={quote(day.replace('-', ''))}&Area={quote(partner)}&Taric={quote(code)}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="AB TARIC sonuçlarını resmî ekranla karşılaştırma listesi üretir.")
    parser.add_argument("--origin", default="TR", help="Menşe ülke ISO kodu (varsayılan TR)")
    parser.add_argument("--codes", nargs="*", default=list(DEFAULT_CODES), help="10 haneli TARIC kodları")
    parser.add_argument("--refresh", action="store_true", help="Arşivi yok sayıp yeniden sorgula (ücretlidir)")
    args = parser.parse_args()

    engine = EuTaricEngine()
    status = engine.status()
    if not status["enabled"]:
        print("AB TARIC sorgusu kapalı. EU_TARIC_ENABLED=1 ve geçerli bir APIFY_TOKEN gerekir.")
        print(f"Durum: {status}")
        await engine.close()
        return 2

    failures = 0
    try:
        for code in args.codes:
            result = await engine.lookup(code, origin=args.origin, refresh=args.refresh)
            summary = result.summary or {}
            print("=" * 78)
            print(f"KOD {code}  ({args.origin})   durum: {result.status}"
                  f"{'  [arşivden]' if result.from_archive else ''}")
            if result.status != "ok":
                failures += 1
                for warning in result.warnings:
                    print(f"  ! {warning}")
                continue
            print(f"  eşya            : {summary.get('goods_description', '')[:90]}")
            print(f"  üçüncü ülke     : {summary.get('mfn_rate')}")
            print(f"  {args.origin} için oran     : {summary.get('partner_rate')} ({summary.get('partner_rate_kind')})")
            print(f"  gereken belge   : {', '.join(summary.get('required_documents') or []) or '—'}")
            extras = summary.get("additional_duties") or []
            if extras:
                labels = [f"{item.get('measure_type') or item.get('kind')} {item.get('duty_text') or ''}".strip() for item in extras[:4]]
                print("  ek vergiler     : " + ", ".join(labels))
            print(f"  durum           : {summary.get('rate_status')}   anlık görüntü: {summary.get('snapshot_month')}")
            print(f"  resmî ekranda doğrula: {official_link(code, args.origin, str(summary.get('snapshot_date') or ''))}")
    finally:
        await engine.close()

    print("=" * 78)
    print(f"{len(args.codes)} kod işlendi, {failures} tanesi sonuç vermedi.")
    print("Her satırı resmî TARIC ekranında açıp üçüncü ülke vergisi ile menşe oranını karşılaştırın.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
