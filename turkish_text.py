"""Türkçe metin karşılaştırma yardımcıları.

Tek bir tuzak için var ve o tuzak gerçek veride iki ayrı modülü birden vurdu:

Türkçe'nin iki I'sı Python'un varsayılan küçültmesiyle uyuşmuyor ve **iki yönden** bozuk
sonuç veriyor::

    >>> "İTHALAT".lower()             # noktalı İ: i + U+0307 birleşen nokta
    'i̇thalat'
    >>> "ithalat" in "İTHALAT".lower()
    False
    >>> "KIYMETİ".lower()             # noktasız I: ı yerine ASCII i
    'kiymeti̇'
    >>> "kıymet" in "KIYMETİ".lower()
    False

Yani ``str.lower()`` ile yapılan basit bir alt dize karşılaştırması, büyük harfli Türkçe
metinde **sessizce başarısız** olur — hem noktalı hem noktasız I'da. İkinci yarısı gerçek
bir terimi vuruyordu: içtihat ilgi terimlerinden ``kıymet``, kaynağın büyük harfli
``GÜMRÜK KIYMETİ`` etiketiyle eşleşmiyordu. Bulunduğu iki yer:

* ``ictihat`` — Bedesten'in konu etiketleri tamamı büyük harf geliyor
  (``GÜMRÜK TARİFE İSTATİSTİK POZİSYONU``), bu yüzden ilgi süzgeci çalışmıyordu;
* ``resmi_gazete`` — belge başlıkları büyük harf (``TEBLİĞ``, ``YÖNETMELİK``), bu yüzden
  metin kalitesi ölçümündeki çapa ifadeler eşleşmiyor ve temiz belgeler ``suspect``
  işaretleniyordu.

Aynı iki satırı iki dosyada tutmak, birinde düzeltilip diğerinde kalan bir hata demek
olurdu; bu yüzden katlama burada, tek yerde duruyor.

Not: ``str.casefold()`` bu sorunu çözmez — o da ``İ`` için ``i`` + U+0307 üretir.
"""

from __future__ import annotations

#: U+0307 COMBINING DOT ABOVE — ``"İ".lower()`` çıktısının ikinci karakteri. Kaynakta
#: kaçış dizisiyle yazılır: görünmez bir birleşen karakteri koda gömmek okunmaz olurdu.
COMBINING_DOT_ABOVE = "̇"

#: Türkçe kuralı: ``İ`` → ``i`` ve ``I`` → ``ı``. Küçültmeden **önce** uygulanır, yoksa
#: Python ``İ``'yi iki karaktere böler ve ``I``'yı ASCII ``i``'ye düşürür.
_TURKISH_LOWER = str.maketrans({"İ": "i", "I": "ı"})

__all__ = ["COMBINING_DOT_ABOVE", "fold"]


def fold(text: str) -> str:
    """Karşılaştırma için Türkçe duyarlı küçültme.

    ``fold("İTHALAT")`` → ``"ithalat"`` ve ``fold("KIYMETİ")`` → ``"kıymeti"``, yani
    ``"ithalat" in fold(...)`` ve ``"kıymet" in fold(...)`` beklendiği gibi çalışır.

    Ölçüt alt dize karşılaştırmasıdır. Yan etki bilinçli: İngilizce bir sözcükteki büyük
    ``I`` da ``ı``'ya döner (``fold("IMPORT") == "ımport"``). Bu işlev Türkçe resmî metinde
    terim aramak için var; harf-harf eşitlik veya arama sıralaması için değil.
    """
    return (text or "").translate(_TURKISH_LOWER).lower().replace(COMBINING_DOT_ABOVE, "")
