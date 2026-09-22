"""İmajın çalışması için gereken dosyalar Dockerfile'a gerçekten giriyor mu.

**Bu testin var olma sebebi.** PR #80 `tariff_nomenclature_sources.json` dosyasını ekledi
ama Dockerfile künye dosyalarını tek tek kopyaladığı için o dosya imaja hiç girmedi.
Sonuç: kapta `NomenclatureEngine()` içe aktarma anında `FileNotFoundError` fırlattı,
`app` modülü hiç yüklenmedi, sağlık kontrolü geçmedi ve Coolify **her denemede** eski
sürüme geri döndü. Kod, testler ve CI'ın tamamı yeşildi; kusur yalnız paketlemedeydi ve
hiçbir test onu göremiyordu.

**Neden uygulamayı "eksik dosyaya dayanıklı" yapmadık.** Eksik künyede motorun sessizce
devre dışı kalması dağıtımı "başarılı" gösterirdi ve özellik ölü olurdu — bu depoda en
pahalıya mal olan hata sınıfı tam olarak bu (bkz. sessizce ölü hibrit indeks). Kabın
gürültülü çökmesi ve geri alınması **doğru** davranıştır: üretim hiçbir an bozuk kod
sunmadı. Düzeltilmesi gereken şey paketlemeydi, dayanıklılık değil.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"


def _copy_patterns() -> list[str]:
    """Dockerfile'daki `COPY` kaynak desenleri (hedef yol hariç)."""
    patterns: list[str] = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = stripped.split()[1:]
        # Son parça hedef; `--from=` gibi bayraklar atlanır.
        patterns.extend(p for p in parts[:-1] if not p.startswith("--"))
    return patterns


def _tracked_root_files(suffix: str) -> list[str]:
    """Kök dizinde git tarafından takip edilen, verilen uzantıdaki dosyalar.

    Git yoksa (ör. ``git archive`` ile ayrı bir dizine çıkarılmış ağaçta doğrulama
    yapılırken) test **atlanır**, hata vermez: bu testin ölçtüğü şey depo içeriği ile
    Dockerfile arasındaki tutarlılık, ve depo bilgisi olmadan o ölçülemez. Sessizce
    yeşile dönmemesi için atlama açıkça raporlanır.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", f"*{suffix}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise unittest.SkipTest(f"git dosya listesi okunamadı: {error}") from error
    return sorted(name for name in out.split() if "/" not in name)


def _covered(name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern == name:
            return True
        if "*" in pattern:
            regex = "^" + re.escape(pattern).replace(r"\*", "[^/]*") + "$"
            if re.match(regex, name):
                return True
    return False


class DockerfilePackagingTests(unittest.TestCase):
    def test_every_tracked_root_json_is_copied(self) -> None:
        """Asıl gerileme kilidi: kökteki her künye dosyası imaja girmeli.

        Bir sonraki `*_sources.json` eklendiğinde bu test, dağıtım değil, CI kırmızıya
        döner — yani hata saatler sonra canlıda değil, saniyeler içinde yakalanır.
        """
        patterns = _copy_patterns()
        missing = [name for name in _tracked_root_files(".json") if not _covered(name, patterns)]
        self.assertEqual(
            missing,
            [],
            f"Dockerfile bu kök JSON dosyalarını kopyalamıyor: {missing}. "
            "Kapta FileNotFoundError ile açılışta çöker.",
        )

    def test_every_tracked_root_python_module_is_copied(self) -> None:
        """Aynı kilit Python modülleri için; `COPY *.py` bunu zaten sağlıyor."""
        patterns = _copy_patterns()
        missing = [name for name in _tracked_root_files(".py") if not _covered(name, patterns)]
        self.assertEqual(missing, [], f"Dockerfile bu modülleri kopyalamıyor: {missing}")

    def test_nomenclature_seed_file_is_tracked_and_copied(self) -> None:
        """Bu dağıtımı düşüren tam dosyayı adıyla kilitle."""
        name = "tariff_nomenclature_sources.json"
        self.assertIn(name, _tracked_root_files(".json"), f"{name} git'te takip edilmiyor")
        self.assertTrue(
            _covered(name, _copy_patterns()),
            f"{name} Dockerfile tarafından kopyalanmıyor — kap açılışta çöker",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
