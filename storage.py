"""Veri diskinin görünürlüğü ve yeri doldurulamaz veritabanlarının yedeği.

Ürünün tüm kalıcı verisi ``MEVZUAT_DATA_DIR`` altındaki SQLite dosyalarında
durur. Bu iki riski sessiz bırakıyordu:

1. **Disk dolarsa** SQLite yazamaz; eşitleme sessizce başarısız olur, kullanıcı
   bunu ancak veri eskidiğinde fark eder. Doluluk hiçbir uçta görünmüyordu.
2. **Veritabanlarının bir kısmı yeniden indirilemez.** Kullanıcı hesapları ve
   kanıt dosyaları (``users``), ücret ödenerek toplanan AB TARIC arşivi
   (``eu_taric``), değişiklik defteri (``changes``) ve resmî kaynakların artık
   yayımlamadığı geçmiş anlık görüntüler (``tariff``, ``controls``) kaybolursa
   geri getirilemez. Buna karşılık BK/ABD/İsviçre tarifesi, AB tüzükleri, EBTI
   kararları ve hibrit indeks resmî kaynaktan **ücretsiz** yeniden kurulur.

Yedek bu ayrımı esas alır: varsayılan olarak yalnız yeri doldurulamaz olanlar
yedeklenir. Yeniden indirilebilen (ve en büyük) dosyaları yedeklemek diskteki
yeri iki katına çıkarır, yani asıl riski — dolu diski — büyütürdü.

Kopya ``VACUUM INTO`` ile alınır: SQLite'ın kendi atomik kopyalama komutu, açık
bağlantılar yazarken bile tutarlı tek bir dosya üretir. Düz dosya kopyası
(``cp``) WAL dosyası yüzünden yarım/bozuk kopya verebilirdi.

**Dürüst sınır:** aynı diskteki yedek, diskin tamamen kaybolmasına karşı
korumaz; kazara silme, bozulma ve hatalı göçe karşı korur. Sunucu dışına almak
için yönetim panelindeki indirme bağlantısı kullanılır — kopyayı yöneticinin
kendi makinesine indirmek, bu kurulumda ücret doğurmayan tek gerçek dış
yedektir.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = "backups"
# Yedek dosya adı: <veritabanı>.<zaman damgası>.sqlite3
# Alt çizgi şart: `eu_taric` gibi adlar kalıba uymazsa kopya alınır ama listelenmez,
# dönüşüme girmez (sonsuza kadar birikir) ve indirilemez.
_BACKUP_NAME_RE = re.compile(r"^(?P<dataset>[a-z0-9_-]+)\.(?P<stamp>\d{8}T\d{6}Z)\.sqlite3$")


@dataclass(frozen=True)
class DatabaseSpec:
    """Bir SQLite dosyasının kimliği ve kaybedilirse ne olacağı."""

    name: str
    filename: str
    label: str
    replaceable: bool  # True: resmî kaynaktan ücretsiz yeniden kurulur
    note: str


# Sıra rapordaki sırayı belirler: önce yeri doldurulamaz olanlar.
DATABASES: tuple[DatabaseSpec, ...] = (
    DatabaseSpec(
        "users", "users.sqlite3", "Kullanıcılar, abonelikler, kanıt dosyaları",
        False, "Hesaplar, kota kayıtları, kaydedilmiş ön değerlendirmeler ve denetim günlüğü.",
    ),
    DatabaseSpec(
        "eu_taric", "eu_taric.sqlite3", "AB TARIC oran arşivi (ücretli)",
        False, "Kod × ülke başına ücret ödenerek toplandı; kaybolursa aynı para yeniden ödenir.",
    ),
    DatabaseSpec(
        "changes", "changes.sqlite3", "Değişiklik defteri",
        False, "Satır düzeyinde geçmiş fark kaydı; kaynaktan yeniden türetilemez.",
    ),
    DatabaseSpec(
        "tariff", "tariff.sqlite3", "Türk tarife cetveli anlık görüntüleri",
        False, "Geçmiş sürümler resmî sitede artık yayımlanmıyor; tarihli sorgunun dayanağı.",
    ),
    DatabaseSpec(
        "controls", "controls.sqlite3", "İthalat/ihracat kontrol tebliğleri",
        False, "Geçmiş tebliğ sürümleri; aynı şekilde yeniden indirilemez.",
    ),
    DatabaseSpec(
        "foreign_tariff", "foreign_tariff.sqlite3", "BK / İsviçre / ABD tarifesi",
        True, "Resmî açık API'lerden ücretsiz yeniden kurulur.",
    ),
    DatabaseSpec(
        "trade_measures", "trade_measures.sqlite3", "Ticaret politikası önlemleri",
        True, "Bakanlık dosyalarından ücretsiz yeniden kurulur.",
    ),
    DatabaseSpec(
        "ebti_decisions", "ebti_decisions.sqlite3", "AB BTB (EBTI) kararları",
        True, "Komisyonun günlük yayınından yeniden kurulur (dolum zaman alır).",
    ),
    DatabaseSpec(
        "classification_evidence", "classification-evidence.sqlite3", "AB sınıflandırma tüzükleri",
        True, "EUR-Lex'ten ücretsiz yeniden kurulur.",
    ),
    DatabaseSpec(
        "hybrid_index", "hybrid_index.sqlite3", "Hibrit arama indeksi",
        True, "Tamamen türetilmiş veri; diğer veritabanlarından yeniden üretilir.",
    ),
)

DEFAULT_BACKUP_DATASETS: tuple[str, ...] = tuple(item.name for item in DATABASES if not item.replaceable)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or ("1" if default else "0")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    default_root = Path.home() / ".cache" / "mevzuat-mcp"
    return Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or default_root)


def _file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _modified_at(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
    except OSError:
        return None


def parse_backup_name(name: str) -> tuple[str, str] | None:
    """``users.20260916T201208Z.sqlite3`` → ``("users", "2026-09-16T20:12:08+00:00")``."""
    match = _BACKUP_NAME_RE.match(name)
    if not match:
        return None
    stamp = match.group("stamp")
    try:
        moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return match.group("dataset"), moment.isoformat(timespec="seconds")


def resolve_backup_file(data_dir: str | Path | None, name: str) -> Path | None:
    """İndirme uçları için: yalnız yedek dizinindeki gerçek bir yedek dosyasını verir.

    Ad biçimi doğrulanır **ve** çözümlenen yolun üst dizini yedek dizinine eşit
    olmalıdır; ``..`` ya da mutlak yol içeren istek sessizce reddedilir.
    """
    if not name or parse_backup_name(name) is None:
        return None
    directory = (resolve_data_dir(data_dir) / BACKUP_DIR_NAME).resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory or not candidate.is_file():
        return None
    return candidate


class StorageService:
    """Disk doluluğu, veritabanı envanteri ve dönüşümlü yedek."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        disk_usage: Callable[[str], Any] = shutil.disk_usage,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        keep: int | None = None,
        datasets: tuple[str, ...] | None = None,
    ) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self._disk_usage = disk_usage
        self.enabled = _env_flag("BACKUP_ENABLED", True) if enabled is None else enabled
        self.interval_seconds = (
            _env_int("BACKUP_INTERVAL_SECONDS", 86_400, minimum=3600) if interval_seconds is None else interval_seconds
        )
        self.keep = _env_int("BACKUP_KEEP", 2, minimum=1) if keep is None else max(1, keep)
        self.datasets = datasets if datasets is not None else self._configured_datasets()
        self._lock = asyncio.Lock()
        self._last_result: dict[str, Any] | None = None

    @staticmethod
    def _configured_datasets() -> tuple[str, ...]:
        raw = (os.environ.get("BACKUP_DATASETS") or "").strip()
        if not raw:
            return DEFAULT_BACKUP_DATASETS
        known = {item.name for item in DATABASES}
        chosen = tuple(part.strip() for part in raw.split(",") if part.strip() in known)
        return chosen or DEFAULT_BACKUP_DATASETS

    # ---- okuma
    def disk(self) -> dict[str, Any]:
        """Veri diskinin toplam/kullanılan/boş alanı; okunamazsa boş rapor."""
        try:
            usage = self._disk_usage(str(self.data_dir))
        except OSError as exc:
            logger.warning("Disk kullanımı okunamadı: %s", exc)
            return {"available": False, "total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "percent_used": 0.0}
        total = int(usage.total)
        used = int(usage.used)
        free = int(usage.free)
        return {
            "available": True,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "percent_used": round(used / total * 100, 1) if total else 0.0,
        }

    def databases(self) -> list[dict[str, Any]]:
        """Her veritabanının boyutu (WAL dâhil) ve kaybedilirse geri gelip gelmeyeceği."""
        rows: list[dict[str, Any]] = []
        for spec in DATABASES:
            path = self.data_dir / spec.filename
            main = _file_bytes(path)
            wal = _file_bytes(path.with_name(path.name + "-wal")) + _file_bytes(path.with_name(path.name + "-shm"))
            rows.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "filename": spec.filename,
                    "exists": path.is_file(),
                    "bytes": main,
                    "wal_bytes": wal,
                    "total_bytes": main + wal,
                    "modified_at": _modified_at(path),
                    "replaceable": spec.replaceable,
                    "backed_up": spec.name in self.datasets,
                    "note": spec.note,
                }
            )
        return rows

    def backups(self) -> list[dict[str, Any]]:
        """Yedek dizinindeki kopyalar, en yeniden en eskiye."""
        directory = self.data_dir / BACKUP_DIR_NAME
        if not directory.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in directory.iterdir():
            parsed = parse_backup_name(path.name)
            if parsed is None or not path.is_file():
                continue
            dataset, created_at = parsed
            rows.append(
                {"name": path.name, "dataset": dataset, "created_at": created_at, "bytes": _file_bytes(path)}
            )
        rows.sort(key=lambda row: (row["created_at"], row["name"]), reverse=True)
        return rows

    def report(self) -> dict[str, Any]:
        """Panelin ve /health'in okuduğu tek rapor."""
        disk = self.disk()
        databases = self.databases()
        backups = self.backups()
        data_bytes = sum(row["total_bytes"] for row in databases)
        backup_bytes = sum(row["bytes"] for row in backups)
        warnings: list[str] = []
        if disk["available"] and disk["percent_used"] >= 90:
            warnings.append(
                f"Veri diski %{disk['percent_used']} dolu. Dolarsa eşitleme veri yazamaz; "
                "yeniden kurulabilir veritabanları (hibrit indeks, BK/ABD arşivi) silinerek yer açılabilir."
            )
        elif disk["available"] and disk["percent_used"] >= 80:
            warnings.append(f"Veri diski %{disk['percent_used']} dolu; yakından izleyin.")
        missing = [
            spec.name for spec in DATABASES
            if spec.name in self.datasets and not (self.data_dir / spec.filename).is_file()
        ]
        covered = {row["dataset"] for row in backups}
        unprotected = [name for name in self.datasets if name not in covered and name not in missing]
        if unprotected:
            warnings.append("Şu veritabanlarının henüz yedeği yok: " + ", ".join(unprotected) + ".")
        return {
            "data_dir_name": self.data_dir.name,
            "disk": disk,
            "databases": databases,
            "data_bytes": data_bytes,
            "backups": backups,
            "backup_bytes": backup_bytes,
            "backup": {
                "enabled": self.enabled,
                "interval_seconds": self.interval_seconds,
                "keep": self.keep,
                "datasets": list(self.datasets),
                "last_run": self._last_result,
                "note": (
                    "Yedek aynı diskte tutulur: kazara silme ve bozulmaya karşı korur, diskin tamamen "
                    "kaybolmasına karşı korumaz. Sunucu dışına almak için yedeği indirin."
                ),
            },
            "warnings": warnings,
        }

    # ---- yazma
    def _has_room(self, source_bytes: int, disk: dict[str, Any]) -> bool:
        """Yedek diski doldurmamalı: kopya kadar yer + %10 emniyet payı aranır.

        Yedek almak için yer yoksa yedek almamak doğrudur; zorlamak, korumaya
        çalıştığımız hatayı (dolu disk) biz üretirdik.
        """
        if not disk["available"]:
            return True  # ölçemiyorsak engellemeyiz; hata zaten kopyalamada görünür
        headroom = int(disk["total_bytes"] * 0.10)
        return disk["free_bytes"] - source_bytes >= headroom

    def create_backup(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Yapılandırılmış veritabanlarının tutarlı kopyasını alır ve eskileri siler."""
        moment = now or datetime.now(UTC)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
        directory = self.data_dir / BACKUP_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:  # paylaşılan birimlerde izin değiştirilemeyebilir
            pass
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        errors: list[str] = []
        specs = {item.name: item for item in DATABASES}
        for name in self.datasets:
            spec = specs.get(name)
            if spec is None:
                continue
            source = self.data_dir / spec.filename
            if not source.is_file():
                skipped.append({"dataset": name, "reason": "veritabanı henüz oluşmamış"})
                continue
            if not self._has_room(_file_bytes(source), self.disk()):
                skipped.append({"dataset": name, "reason": "diskte yeterli boş alan yok"})
                errors.append(f"{name}: diskte yer olmadığı için yedek alınamadı.")
                continue
            target = directory / f"{name}.{stamp}.sqlite3"
            try:
                self._vacuum_into(source, target)
            except (sqlite3.Error, OSError) as exc:
                errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
                target.unlink(missing_ok=True)
                continue
            try:
                target.chmod(0o600)
            except OSError:
                pass
            created.append({"dataset": name, "name": target.name, "bytes": _file_bytes(target)})
        removed = self._rotate(directory)
        result = {
            "at": moment.isoformat(timespec="seconds"),
            "created": created,
            "skipped": skipped,
            "removed": removed,
            "errors": errors,
        }
        self._last_result = result
        return result

    @staticmethod
    def _vacuum_into(source: Path, target: Path) -> None:
        """``VACUUM INTO``: açık yazarlar varken bile tutarlı tek dosya üretir."""
        target.unlink(missing_ok=True)
        connection = sqlite3.connect(source, timeout=60)
        try:
            connection.execute("VACUUM INTO ?", (str(target),))
        finally:
            connection.close()

    def _rotate(self, directory: Path) -> list[str]:
        """Veritabanı başına en yeni ``keep`` kopyayı bırakır."""
        by_dataset: dict[str, list[tuple[str, Path]]] = {}
        for path in directory.iterdir():
            parsed = parse_backup_name(path.name)
            if parsed is None or not path.is_file():
                continue
            by_dataset.setdefault(parsed[0], []).append((parsed[1], path))
        removed: list[str] = []
        for items in by_dataset.values():
            items.sort(key=lambda item: item[0], reverse=True)
            for _, path in items[self.keep:]:
                try:
                    path.unlink()
                    removed.append(path.name)
                except OSError as exc:
                    logger.warning("Eski yedek silinemedi (%s): %s", path.name, exc)
        return removed

    async def backup_now(self) -> dict[str, Any]:
        """Eş zamanlı iki yedek koşusunu engelleyerek kopyayı iş parçacığında alır."""
        async with self._lock:
            return await asyncio.to_thread(self.create_backup)

    async def periodic_backup_loop(self, *, initial_delay: float = 600.0) -> None:
        """Günlük yedek döngüsü; hata sunucuyu durdurmaz."""
        await asyncio.sleep(initial_delay)
        while True:
            try:
                result = await self.backup_now()
                if result["errors"]:
                    logger.warning("Yedek kısmen alındı: %s", "; ".join(result["errors"])[:400])
                else:
                    logger.info(
                        "Yedek alındı: %s kopya, %s eski kopya silindi",
                        len(result["created"]), len(result["removed"]),
                    )
            except Exception:  # noqa: BLE001 – yedek döngüsü sunucuyu durdurmaz
                logger.exception("Yedek alınamadı")
            await asyncio.sleep(self.interval_seconds)


__all__ = [
    "BACKUP_DIR_NAME",
    "DATABASES",
    "DEFAULT_BACKUP_DATASETS",
    "DatabaseSpec",
    "StorageService",
    "parse_backup_name",
    "resolve_backup_file",
    "resolve_data_dir",
]
