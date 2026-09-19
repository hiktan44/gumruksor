"""Arka plan işleri kütüğü: hangisi çalışıyor, hangisi neden kapalı, hangisi çöktü.

Sunucu açılışta bir dizi sonsuz döngü başlatır (resmî kaynak eşitlemeleri, arşiv
dolumları, indeks tazeleme, bildirim gönderimi). Bu döngülerin iki sessiz arızası var
ve ikisini de bu kütük görünür kılar:

* **Kapalı iş görünmez.** ``if FLAG: BACKGROUND_LOOPS.append(...)`` deseninde bayrak
  kapalıysa iş listeye hiç girmez; panelde de hiç görünmez. O zaman "AB TARIC dolumu
  neden ilerlemiyor" sorusunun cevabı hiçbir ekranda yazmaz. Bu yüzden kapalı işler de
  **sebebiyle birlikte** kaydedilir (``declare(enabled=False, reason=...)``).
* **Çöken döngü sessizce ölür.** ``asyncio.create_task`` ile başlatılan bir görev
  istisnayla biterse süreç çalışmaya devam eder; yalnız o iş durur. Kütük her göreve
  bir bitiş geri çağrısı takar ve istisnanın **türünü** saklar.

Gizli değer saklanmaz: hata metni değil yalnız istisna sınıfının adı ve kısaltılmış
mesajı tutulur (aynı ilke tanılama uçlarında da geçerli).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = ["JOB_CATALOGUE", "JobRecord", "JobRegistry", "registry"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# İş adı → insan tarafından okunabilir künye. Ad, ``asyncio.Task`` adıyla birebir aynıdır.
JOB_CATALOGUE: dict[str, dict[str, str]] = {
    "official-tariff-refresh": {
        "label": "Türk tarife cetveli",
        "purpose": "İthalat Rejimi (3350) ve İGV (3351) eklerini resmî kaynaktan tazeler; yeni anlık görüntü inceleme kapısına düşer.",
        "cost": "free",
    },
    "official-import-controls-refresh": {
        "label": "ÜGD kontrol tebliğleri",
        "purpose": "Ürün Güvenliği ve Denetimi tebliğlerinin Ek listelerini ve ihracat kontrol listelerini eşitler.",
        "cost": "free",
    },
    "official-classification-evidence-refresh": {
        "label": "AB sınıflandırma tüzükleri",
        "purpose": "Sınıflandırma kanıtı sayfalarını indirir ve tam metin aramasına işler.",
        "cost": "free",
    },
    "ticaret-catalog-refresh": {
        "label": "Ticaret Bakanlığı kataloğu",
        "purpose": "Bakanlık duyuru ve mevzuat sayfalarının dizinini tazeler.",
        "cost": "free",
    },
    "trade-measures-sync": {
        "label": "Ticaret politikası önlemleri",
        "purpose": "Damping, korunma, gözetim ve İthalat Tebliğleri verisini günlük eşitler.",
        "cost": "free",
    },
    "vat-lists-sync": {
        "label": "KDV listeleri",
        "purpose": "2007/13033 sayılı Karar eki (I) ve (II) sayılı listeleri resmî metinden tazeler.",
        "cost": "free",
    },
    "resmi-gazete-archive": {
        "label": "Resmî Gazete arşivi",
        "purpose": "Gümrüğü ilgilendiren Resmî Gazete belgelerini bugünden geriye doğru kalıcı "
        "arşive alır; her gün için normal sayı ve mükerrer sayılar birlikte taranır.",
        "cost": "free",
    },
    "eu-vat-sync": {
        "label": "AB KDV oranları (TEDB)",
        "purpose": "AB-27 KDV oranlarını TEDB'den çekmeyi dener; veri gelmezse uzman teyitli tohum korunur.",
        "cost": "free",
    },
    "foreign-tariff-sync": {
        "label": "BK / İsviçre tarife nomenklatürü",
        "purpose": "Birleşik Krallık açık API'si ve İsviçre resmî nomenklatürünü eşitler.",
        "cost": "free",
    },
    "uk-measures-archive": {
        "label": "BK oran arşivi dolumu",
        "purpose": "Birleşik Krallık ölçülerini kod kod yerel arşive çeker.",
        "cost": "free",
    },
    "ebti-sync": {
        "label": "AB BTB (EBTI) kararları",
        "purpose": "Komisyonun günlük yayımladığı bağlayıcı tarife bilgisi kararlarını arşive alır.",
        "cost": "free",
    },
    "eu-taric-fill": {
        "label": "AB TARIC toplu dolumu",
        "purpose": "Ücretli dış aktörle kod × ülke çiftlerini arşive çeker. Her sorgu ücretlidir; aylık tavan ve yaş tabanlı tazeleme uygulanır.",
        "cost": "paid",
    },
    "access2markets-fill": {
        "label": "Access2Markets toplu dolumu",
        "purpose": "Aynı AB verisinin ücretsiz kaynağından toplu arşiv doldurur.",
        "cost": "free",
    },
    "hybrid-index-refresh": {
        "label": "Hibrit arama indeksi",
        "purpose": "Onaylı anlık görüntüleri BM25 + gömme indeksine yeniden besler; içeriği değişmeyen belge yeniden gömülmez.",
        "cost": "free",
    },
    "change-ledger-backfill": {
        "label": "Değişiklik defteri geriye dolumu",
        "purpose": "Mevcut anlık görüntü zincirinden geçmiş değişiklikleri deftere yazar. Tek seferliktir; bittiğinde durur.",
        "cost": "free",
        "one_shot": "1",
    },
    "storage-backup": {
        "label": "Otomatik yedek",
        "purpose": "Yeniden indirilemeyen veritabanlarının yedeğini alır.",
        "cost": "free",
    },
    "watchlist-notifications": {
        "label": "İzleme listesi bildirimleri",
        "purpose": "İzlenen GTİP'lerde değişiklik olduğunda kullanıcıya e-posta gönderir.",
        "cost": "free",
    },
    "review-notifications": {
        "label": "İnceleme kuyruğu bildirimleri",
        "purpose": "Bekleyen veri incelemesi için yöneticiye hatırlatma gönderir.",
        "cost": "free",
    },
}


def _describe(name: str) -> dict[str, str]:
    known = JOB_CATALOGUE.get(name)
    if known:
        return dict(known)
    # Kütükte olmayan bir iş: adını gizlemek yerine "künyesi yok" diye göster.
    return {"label": name, "purpose": "Bu iş için künye tanımlanmamış.", "cost": "free"}


@dataclass
class JobRecord:
    name: str
    enabled: bool = True
    disabled_reason: str = ""
    interval_seconds: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    state: str = "declared"  # declared | disabled | running | finished | cancelled | failed
    error: str | None = None
    error_at: str | None = None
    _task: Any = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        info = _describe(self.name)
        return {
            "name": self.name,
            "label": info["label"],
            "purpose": info["purpose"],
            "cost": info.get("cost", "free"),
            "one_shot": info.get("one_shot") == "1",
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason or None,
            "state": self.state,
            "interval_seconds": self.interval_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "error_at": self.error_at,
        }


class JobRegistry:
    """Süreç ömrü boyunca yaşayan basit kütük. Kalıcı depo yok: durum çalışma anına aittir."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}

    def declare(
        self,
        name: str,
        *,
        enabled: bool = True,
        reason: str = "",
        interval_seconds: float | None = None,
    ) -> JobRecord:
        """İşi kütüğe yaz. Kapalı iş de yazılır — görünmeyen iş teşhis edilemez."""
        record = self._jobs.get(name) or JobRecord(name=name)
        record.enabled = bool(enabled)
        record.disabled_reason = reason if not enabled else ""
        record.interval_seconds = interval_seconds
        if not enabled:
            record.state = "disabled"
        self._jobs[name] = record
        return record

    def track(self, name: str, task: Any) -> None:
        """Başlatılmış göreve bitiş geri çağrısı tak; sessizce ölen döngü görünür olsun."""
        record = self._jobs.get(name) or self.declare(name)
        record._task = task
        record.state = "running"
        record.started_at = _now()
        record.finished_at = None
        task.add_done_callback(lambda finished, key=name: self._finished(key, finished))

    def _finished(self, name: str, task: Any) -> None:
        record = self._jobs.get(name)
        if record is None:
            return
        record.finished_at = _now()
        if task.cancelled():
            record.state = "cancelled"
            return
        error = task.exception()
        if error is None:
            record.state = "finished"
            return
        record.state = "failed"
        # Gizli değer sızmasın: yalnız sınıf adı ve kısaltılmış mesaj.
        record.error = f"{type(error).__name__}: {str(error)[:200]}"
        record.error_at = record.finished_at

    def snapshot(self) -> list[dict[str, Any]]:
        rows = [record.as_dict() for record in self._jobs.values()]
        order = {"failed": 0, "disabled": 1, "cancelled": 2, "declared": 3, "running": 4, "finished": 5}
        rows.sort(key=lambda row: (order.get(row["state"], 9), row["label"]))
        return rows

    def summary(self) -> dict[str, Any]:
        rows = self.snapshot()
        return {
            "total": len(rows),
            "running": sum(1 for row in rows if row["state"] == "running"),
            "failed": sum(1 for row in rows if row["state"] == "failed"),
            "disabled": sum(1 for row in rows if row["state"] == "disabled"),
            "finished": sum(1 for row in rows if row["state"] == "finished"),
            "paid": [row["name"] for row in rows if row["cost"] == "paid" and row["state"] == "running"],
        }

    def reset(self) -> None:
        """Yalnız testler için: kütüğü boşalt."""
        self._jobs.clear()


registry = JobRegistry()
