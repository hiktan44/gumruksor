"""TypeSafe AI "Jev" System One modeli için dar ve güvenli istemci.

**Neden var.** Sınıflandırma hattı 6 hanede kalan bir adayı, cetvelde o pozisyonun birden
fazla CN8 alt satırı varsa daraltamıyor: tek-çocuk daraltması yalnız seçim gerektirmeyen
vakayı çözer. Jev'in ``choice`` ilkeli tam bu işi yapar — sabit bir seçenek listesinden birini
seçer ve kalibre edilmiş olasılık ile güven döndürür. Seçenekler bizim resmî cetvelimizden
geldiği için model kod uyduramaz; en fazla bizim verdiğimiz satırlardan birini seçer.

**Kaynak kararı (bilinçli).** Yalnız resmî uç nokta kullanılır:
``https://api.typesafe.ai/v1/systemone``. ``jevai.org`` kendini "resmî ürün sitesi değil"
diye tanımlayan bir topluluk sitesidir, kendi anahtarını verir ve farklı bir API
(``/v1/decisions``) sunar. Gümrük verisi kimliği belirsiz bir aracıya gönderilmez; bu yüzden
adres ortam değişkeniyle **değiştirilemez**. Yalnız model adı ve zaman aşımı ayarlanabilir.

Hiçbir hata metni API anahtarını taşımaz; model yanıtı veri olarak işlenir, talimat değildir.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from security_firewall import redact_text, validate_outbound_url

API_URL = "https://api.typesafe.ai/v1/systemone"
_ALLOWED_HOSTS = frozenset({"api.typesafe.ai"})
_MAX_BYTES = 256_000
# Resmî sınır 255 seçenek; bir HS6'nın CN8 alt satırı pratikte bunun çok altındadır.
MAX_OPTIONS = 255


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(max(value, low), high)


DEFAULT_MODEL = (os.environ.get("TYPESAFE_MODEL") or "jev-latest").strip() or "jev-latest"
# Belgelenmiş gecikme 70-500 ms. Sınır cömert ama kesin: Jev bir iyileştirmedir, gecikmesi
# sınıflandırmayı bekletmemeli.
DEFAULT_TIMEOUT = _env_float("TYPESAFE_TIMEOUT_SECONDS", 4.0, low=0.5, high=15.0)


class JevError(RuntimeError):
    """Jev çağrısı başarısız oldu. Mesaj hiçbir zaman API anahtarını içermez."""


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


def api_key_from_env() -> str:
    return (os.environ.get("TYPESAFE_API_KEY") or "").strip()


class TypeSafeClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        http: httpx.AsyncClient | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._key = (api_key if api_key is not None else api_key_from_env()).strip()
        self._http = http
        self.model = (model or DEFAULT_MODEL).strip() or "jev-latest"
        self.timeout = DEFAULT_TIMEOUT if timeout is None else timeout

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _mask(self, text: str) -> str:
        if self._key:
            text = text.replace(self._key, "***")
        return redact_text(text)

    def _safe_detail(self, response: httpx.Response, limit: int = 200) -> str:
        try:
            body = response.content[:2000].decode("utf-8", "replace")
        except (AttributeError, ValueError):
            return ""
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            error = parsed.get("error") or parsed.get("detail") or parsed.get("message")
            if isinstance(error, dict):
                body = str(error.get("message") or error.get("type") or body)
            elif error:
                body = str(error)
        return self._mask(re.sub(r"\s+", " ", body).strip())[:limit]

    async def choose(
        self,
        state: str,
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, ChoiceAnswer]:
        """``choice`` sorularını tek istekte sorar; yalnız geçerli yanıtları döndürür.

        Bir yanıt, kendi sorusunun seçenekleri arasında olmayan bir değer seçerse **atılır**.
        Seçenek listesi bizim resmî cetvelimizden gelir; listede olmayan bir cevap, modelin
        kod uydurmasıdır ve hiçbir koşulda hatta girmez.
        """
        if not self._key:
            raise JevError("TYPESAFE_API_KEY yapılandırılmamış.")
        if not questions:
            return {}
        options_by_id: dict[str, set[str]] = {}
        for question_id, question in questions.items():
            criteria = question.get("criteria")
            if question.get("type") != "choice" or not isinstance(criteria, dict) or not criteria:
                raise JevError(f"'{question_id}' geçerli bir choice sorusu değil.")
            if len(criteria) > MAX_OPTIONS:
                raise JevError(f"'{question_id}' en fazla {MAX_OPTIONS} seçenek alabilir.")
            options_by_id[question_id] = {str(key) for key in criteria}

        validate_outbound_url(API_URL, allowed_hosts=_ALLOWED_HOSTS)
        payload = {
            # Durum metni sağlayıcıya gitmeden önce gizli değer ve kişisel veriden arındırılır.
            "state": redact_text(str(state or "")),
            "model": self.model,
            "questions": questions,
        }
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            if self._http is not None:
                response = await self._http.post(
                    API_URL, json=payload, headers=headers, follow_redirects=False,
                    timeout=self.timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                    response = await client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise JevError(f"Jev'e ulaşılamadı: {type(exc).__name__}") from None
        except (UnicodeEncodeError, TypeError) as exc:
            # Bozuk anahtar başlığa yazılamazsa anahtar hata metnine sızmasın.
            raise JevError(f"Jev isteği oluşturulamadı: {type(exc).__name__}") from None

        if not response.is_success:
            # Yönlendirme dahil 2xx dışındaki her yanıt başarısızdır; yönlendirme izlenmez.
            detail = self._safe_detail(response)
            raise JevError(
                f"Jev {response.status_code} döndürdü{(': ' + detail) if detail else '.'}"
            )
        content = response.content
        if len(content) > _MAX_BYTES:
            raise JevError("Jev yanıtı beklenenden büyük.")
        try:
            body = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise JevError("Jev yanıtı çözümlenemedi.") from None
        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict):
            raise JevError("Jev yanıtı beklenen biçimde değil.")

        result: dict[str, ChoiceAnswer] = {}
        for question_id, allowed in options_by_id.items():
            answer = answers.get(question_id)
            if not isinstance(answer, dict):
                continue
            choice = str(answer.get("choice") or "")
            if choice not in allowed:
                continue
            try:
                confidence = float(answer.get("confidence"))
            except (TypeError, ValueError):
                continue
            if not 0.0 <= confidence <= 1.0:
                continue
            raw_probabilities = answer.get("probabilities")
            probabilities: dict[str, float] = {}
            if isinstance(raw_probabilities, dict):
                for key, value in raw_probabilities.items():
                    if str(key) not in allowed:
                        continue
                    try:
                        probabilities[str(key)] = float(value)
                    except (TypeError, ValueError):
                        continue
            result[question_id] = ChoiceAnswer(
                choice=choice, confidence=confidence, probabilities=probabilities
            )
        return result

    async def diagnose(self) -> dict[str, Any]:
        """Yönetici tanılaması: anahtar sunucuya ulaştı mı, resmî API'de çalışıyor mu?

        **Müşteri verisi kullanılmaz**; sabit bir sentetik cümle sorulur. Anahtarın kendisi
        asla döndürülmez. ``ok: false`` ve ``401``/``403`` hatası, anahtarın resmî TypeSafe
        anahtarı olmadığını (örneğin jevai.org topluluk sitesinden alındığını) gösterir.
        """
        import time

        report: dict[str, Any] = {
            "configured": self.configured,
            "endpoint": "api.typesafe.ai",
            "model": self.model,
        }
        if not self.configured:
            report["ok"] = False
            report["error"] = "TYPESAFE_API_KEY sunucuda yok (Coolify'da runtime değişkeni olarak ekleyip Restart edin)."
            return report
        started = time.perf_counter()
        try:
            answers = await self.choose(
                "Pamuklu örme kısa kollu tişört.",
                {
                    "probe": {
                        "type": "choice",
                        "instructions": "Bu eşya hangi gruba girer?",
                        "criteria": {"tekstil": "Giyim ve tekstil ürünleri", "elektronik": "Elektrikli cihazlar"},
                    }
                },
            )
        except JevError as exc:
            report["ok"] = False
            report["error"] = str(exc)
        else:
            probe = answers.get("probe")
            report["ok"] = probe is not None
            if probe is not None:
                report["probe_choice"] = probe.choice
                report["probe_confidence"] = probe.confidence
            else:
                report["error"] = "Jev geçerli bir seçim döndürmedi."
        report["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return report
