"""Structured product-page extraction for the "ürün belgesi ekle" flow.

E-ticaret sayfaları (Trendyol, Hepsiburada, Amazon, marka siteleri) ürün
bilgisini görünür metinden çok yapılandırılmış veride taşır: JSON-LD
``Product`` nesneleri, ``og:`` meta etiketleri ve Trendyol'un
``window.__PRODUCT_DETAIL_APP_INITIAL_STATE__`` durumu. Düz metin çıkarımı bu
sayfalarda önce menüleri, kategori ağacını ve çerez uyarılarını okur; 6.000
karakterlik sınır ürün adına gelmeden dolar. Bu modül önce yapılandırılmış
kaynakları okur, bulamazsa gövde metnine geri döner.

Sayfa içeriği güvenilmeyen veridir: yalnızca alan olarak taşınır, hiçbir
talimat olarak yorumlanmaz; LLM'e giden metin ayrıca ``sanitize_untrusted_context``
ve ``redact_text`` süzgeçlerinden geçer.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

MAX_HTML_BYTES = 2_000_000
MAX_ATTRIBUTES = 40
_MAX_FIELD_CHARS = 1_500

# Tarayıcı benzeri başlıklar: birçok e-ticaret sitesi tanınmayan User-Agent'a
# 403 veya boş kabuk sayfa döndürür. Bu bir "gizlenme" değil, sıradan bir
# tarayıcı isteğinin taklididir; robots/anti-bot duvarına takılırsa açıkça
# kullanıcıya bildirilir (bkz. detect_bot_wall).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

_BOT_WALL_MARKERS = (
    "just a moment",
    "attention required",
    "cf-challenge",
    "challenge-platform",
    "access denied",
    "erişim engellendi",
    "robot olmadığınızı",
    "are you a robot",
    "captcha",
    "px-captcha",
    "_incapsula_",
    "distil_r_captcha",
    "bot detection",
    "request unsuccessful",
)

_TRENDYOL_STATE_RE = re.compile(r"__PRODUCT_DETAIL_APP_INITIAL_STATE__\s*=\s*")
_WS_RE = re.compile(r"\s+")


def _clean(value: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = _WS_RE.sub(" ", str(value)).strip()
    return text[:limit]


def _strip_html(value: Any) -> str:
    """Ürün açıklamaları çoğu zaman HTML olarak gelir; etiketleri düşür."""
    text = _clean(value, limit=20_000)
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return _clean(text)


def detect_bot_wall(status_code: int, html_text: str) -> str | None:
    """Return a user-facing explanation when the site blocked automated reading."""
    sample = (html_text or "")[:20_000].lower()
    if status_code in {401, 403, 429, 503} or any(marker in sample for marker in _BOT_WALL_MARKERS):
        return (
            f"Site otomatik okumayı engelledi (HTTP {status_code}). Ürün sayfasındaki başlığı ve "
            "açıklamayı kopyalayıp ürün tanımına yapıştırın ya da sayfayı PDF olarak kaydedip yükleyin."
        )
    return None


# --------------------------------------------------------------------------- JSON-LD


def _iter_jsonld_nodes(soup: BeautifulSoup):
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for key in ("@graph", "mainEntity", "itemListElement", "item"):
                    inner = node.get(key)
                    if isinstance(inner, (dict, list)):
                        stack.append(inner)


def _is_product_node(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(item, str) and item.lower().endswith("product") for item in kinds)


def _name_of(value: Any) -> str:
    if isinstance(value, dict):
        return _clean(value.get("name") or value.get("@id") or "")
    if isinstance(value, list):
        return ", ".join(filter(None, (_name_of(item) for item in value)))[:_MAX_FIELD_CHARS]
    return _clean(value)


def _jsonld_product(soup: BeautifulSoup) -> dict[str, Any]:
    for node in _iter_jsonld_nodes(soup):
        if not _is_product_node(node):
            continue
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not isinstance(offers, dict):
            offers = {}
        attributes: list[tuple[str, str]] = []
        for prop in node.get("additionalProperty") or []:
            if isinstance(prop, dict):
                name, value = _clean(prop.get("name")), _clean(prop.get("value"))
                if name and value:
                    attributes.append((name, value))
        for key in ("material", "color", "size", "pattern", "model", "gtin", "gtin13", "mpn", "sku", "countryOfOrigin"):
            value = _name_of(node.get(key))
            if value:
                attributes.append((key, value))
        return {
            "name": _clean(node.get("name")),
            "brand": _name_of(node.get("brand")),
            "category": _name_of(node.get("category")),
            "description": _strip_html(node.get("description")),
            "price": _clean(offers.get("price") or offers.get("lowPrice")),
            "currency": _clean(offers.get("priceCurrency")),
            "attributes": attributes,
            "source": "json-ld",
        }
    return {}


# --------------------------------------------------------------------------- Trendyol state


def _extract_js_object(text: str, start: int) -> dict[str, Any] | None:
    """Parse the JSON object that starts at ``start`` (brace matching, string-aware)."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(len(text), start + MAX_HTML_BYTES)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _find_key(node: Any, key: str, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node[:20]:
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    return None


def _trendyol_product(html_text: str) -> dict[str, Any]:
    match = _TRENDYOL_STATE_RE.search(html_text)
    if not match:
        return {}
    state = _extract_js_object(html_text, match.end())
    if not state:
        return {}
    product = state.get("product") if isinstance(state.get("product"), dict) else _find_key(state, "product")
    if not isinstance(product, dict):
        return {}
    attributes: list[tuple[str, str]] = []
    for item in product.get("attributes") or []:
        if not isinstance(item, dict):
            continue
        name = _name_of(item.get("key")) or _clean(item.get("name"))
        value = _name_of(item.get("value"))
        if name and value:
            attributes.append((name, value))
    descriptions = [
        _strip_html(entry.get("description"))
        for entry in product.get("contentDescriptions") or []
        if isinstance(entry, dict)
    ]
    description = _strip_html(product.get("description")) or " ".join(filter(None, descriptions))
    price = product.get("price") if isinstance(product.get("price"), dict) else {}
    category = product.get("category") if isinstance(product.get("category"), dict) else {}
    return {
        "name": _clean(product.get("name")),
        "brand": _name_of(product.get("brand")),
        "category": _clean(category.get("name")) if category else _clean(product.get("categoryName")),
        "description": description[:_MAX_FIELD_CHARS],
        "price": _clean(price.get("sellingPrice", {}).get("value") if isinstance(price.get("sellingPrice"), dict) else price.get("sellingPrice")),
        "currency": _clean(price.get("currency")) or ("TRY" if price else ""),
        "attributes": attributes,
        "source": "trendyol-state",
    }


# --------------------------------------------------------------------------- meta / body


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean(tag.get("content"))
    return ""


def _body_text(soup: BeautifulSoup, limit: int) -> str:
    """Visible text without navigation chrome (menus, footers, cookie banners)."""
    for element in soup(["script", "style", "noscript", "template", "svg", "iframe", "nav", "header", "footer", "aside", "form"]):
        element.decompose()
    for element in soup.find_all(attrs={"role": re.compile(r"^(navigation|banner|contentinfo|dialog)$", re.I)}):
        element.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    return _clean(root.get_text(" ", strip=True), limit=limit)


def extract_product_page(html_text: str, url: str, *, max_chars: int = 6_000) -> dict[str, Any]:
    """Return ``{"title", "text", "structured", "extraction"}`` for a fetched product page.

    ``text`` is a Turkish, review-ready summary the user pastes into the
    product description; ``structured`` keeps the raw fields for the UI.
    """
    html_text = (html_text or "")[:MAX_HTML_BYTES]
    soup = BeautifulSoup(html_text, "lxml")
    page_title = _clean(soup.title.string) if soup.title and soup.title.string else ""
    product = _trendyol_product(html_text) or _jsonld_product(soup)
    heading = soup.find("h1")
    h1 = _clean(heading.get_text(" ", strip=True)) if heading else ""
    meta_description = _meta(soup, "og:description", "description", "twitter:description")
    og_title = _meta(soup, "og:title", "twitter:title")

    name = product.get("name") or h1 or og_title or page_title
    brand = product.get("brand") or _meta(soup, "product:brand", "og:brand")
    category = product.get("category")
    description = product.get("description") or meta_description
    attributes = list(product.get("attributes") or [])[:MAX_ATTRIBUTES]
    price = product.get("price") or _meta(soup, "product:price:amount", "og:price:amount")
    currency = product.get("currency") or _meta(soup, "product:price:currency", "og:price:currency")

    lines: list[str] = []
    if name:
        lines.append(f"Ürün adı: {name}")
    if brand:
        lines.append(f"Marka: {brand}")
    if category:
        lines.append(f"Kategori: {category}")
    if description:
        lines.append(f"Açıklama: {description}")
    if attributes:
        lines.append("Özellikler: " + "; ".join(f"{key}: {value}" for key, value in attributes))
    if price:
        lines.append(f"Sayfadaki satış fiyatı: {price} {currency}".strip() + " (gümrük kıymeti değildir)")
    structured = bool(product) or bool(name and (description or attributes))
    text = "\n".join(lines)
    if len(text) < 200:
        # Yapılandırılmış veri yetersiz: gövde metniyle tamamla.
        body = _body_text(soup, max_chars)
        text = f"{text}\n{body}".strip() if text else body
    return {
        "title": (name or page_title or url)[:200],
        "text": text[:max_chars],
        "structured": {
            "name": name,
            "brand": brand,
            "category": category,
            "description": description,
            "attributes": [{"name": key, "value": value} for key, value in attributes],
            "price": price,
            "currency": currency,
            "source": product.get("source") or ("meta" if structured else "text"),
        },
        "extraction": "structured" if structured else "text",
    }


# --------------------------------------------------------------------------- marka / model doğrulama (PRD Faz 3.4)

_TR_FOLD_TABLE = str.maketrans({"İ": "i", "I": "ı", "Ş": "ş", "Ğ": "ğ", "Ü": "ü", "Ö": "ö", "Ç": "ç"})
_COMPACT_RE = re.compile(r"[^0-9a-zçğıöşü]+")


def fold_turkish(value: Any) -> str:
    """Case-insensitive normalisation that keeps Turkish dotted/dotless i apart correctly."""
    text = _WS_RE.sub(" ", str(value or "")).strip()
    return text.translate(_TR_FOLD_TABLE).casefold()


def _compact(value: str) -> str:
    """Drop spaces/punctuation so ``K-9000 XL`` matches ``K9000XL`` on the page."""
    return _COMPACT_RE.sub("", fold_turkish(value))


def _contains(needle: str, haystack: str) -> bool:
    needle_folded, haystack_folded = fold_turkish(needle), fold_turkish(haystack)
    if not needle_folded or not haystack_folded:
        return False
    if needle_folded in haystack_folded:
        return True
    compact_needle = _compact(needle)
    return len(compact_needle) >= 3 and compact_needle in _compact(haystack)


def brand_model_match(brand: str, model: str, page: dict[str, Any]) -> dict[str, Any]:
    """Score how well a user-typed brand + model pair matches an extracted product page.

    The page is untrusted data and is only searched, never interpreted. Score:
    brand in the page's brand/name field 50, elsewhere in the text 35; model in
    the product name 50, elsewhere (attributes/description/text) 35. The
    verdict is ``match`` (>= 85), ``partial`` (>= 35) or ``no_match``. Nothing
    here touches tariff classification.
    """
    structured = page.get("structured") if isinstance(page.get("structured"), dict) else {}
    name = str(structured.get("name") or page.get("title") or "")
    page_brand = str(structured.get("brand") or "")
    attributes = " ; ".join(
        f"{item.get('name', '')}: {item.get('value', '')}"
        for item in (structured.get("attributes") or [])
        if isinstance(item, dict)
    )
    body = " ".join(
        filter(None, [name, page_brand, str(structured.get("description") or ""), attributes, str(page.get("text") or "")])
    )

    brand = _clean(brand, limit=150)
    model = _clean(model, limit=150)
    evidence: list[str] = []
    score = 0
    brand_found = model_found = False
    if brand:
        if _contains(brand, page_brand) or _contains(brand, name):
            score += 50
            brand_found = True
            evidence.append(f"Marka '{brand}' sayfanın ürün adı/marka alanında geçiyor.")
        elif _contains(brand, body):
            score += 35
            brand_found = True
            evidence.append(f"Marka '{brand}' sayfa metninde geçiyor (ürün adı alanında değil).")
        else:
            evidence.append(f"Marka '{brand}' sayfada bulunamadı.")
    if model:
        if _contains(model, name):
            score += 50
            model_found = True
            evidence.append(f"Model '{model}' sayfanın ürün adında geçiyor.")
        elif _contains(model, body):
            score += 35
            model_found = True
            evidence.append(f"Model '{model}' sayfanın özellik/açıklama metninde geçiyor.")
        else:
            evidence.append(f"Model '{model}' sayfada bulunamadı.")
    if not brand and not model:
        evidence.append("Karşılaştırılacak marka veya model girilmedi.")
    verdict = "match" if score >= 85 else "partial" if score >= 35 else "no_match"
    if brand and not model and brand_found:
        verdict = "partial"
        evidence.append("Model girilmediği için yalnızca marka doğrulandı.")
    return {
        "score": score,
        "verdict": verdict,
        "brand_found": brand_found,
        "model_found": model_found,
        "page_brand": page_brand[:150],
        "page_name": name[:200],
        "evidence": evidence,
    }
