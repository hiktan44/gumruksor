# mevzuat_mcp_server_new.py
"""
FastMCP server for mevzuat.gov.tr (direct API).
Supports searching and PDF content extraction for Kanun (laws).
"""
import asyncio
import logging
import numpy as np
from contextlib import asynccontextmanager
from pydantic import Field
from datetime import date
from typing import Callable, Coroutine, Any, Literal, Optional

from fastmcp import FastMCP

from mevzuat_client import MevzuatApiClientNew
from mevzuat_models import (
    MevzuatSearchRequestNew,
    MevzuatSearchResultNew,
    MevzuatArticleContent
)
from article_search import search_articles_by_keyword, ArticleSearchResult, format_search_results, _matches_query, search_plain_text_articles
from ticaret_client import TicaretApiClient
from ticaret_models import (
    TicaretCatalogStatus,
    TicaretContentSearchResult,
    TicaretDocumentContent,
    TicaretSearchResult,
)
from assistant import CustomsAssistant, build_default_tools as build_assistant_tools
from customs_advisor import (
    ClassificationAnswer,
    CustomsAdvisor,
    CustomsEvidencePack,
    CustomsInquiry,
    ProductAttributeAnalysis,
    ProductClassificationRequest,
    ProductClassificationResult,
    decode_image_data_url,
)
from exchange_rates import ExchangeRateError, ExchangeRateService, parse_registration_date
from eylemio_client import EylemioClient, EylemioError, summarise_declaration
from trade_measures import KIND_LABELS as TRADE_MEASURE_LABELS, TradeMeasureEngine, summary_lines as trade_measure_summary
from tax_lists import ExciseTaxIndex, summary_lines as excise_tax_summary
from eu_vat import EuVatRates
from vat_lists import VatRateIndex
from tariff_engine import (
    LandedCostInput,
    TariffDecisionTreeResult,
    TariffEngine,
    TariffLookupResult,
    TariffSyncStatus,
)
from control_engine import ImportControlEngine, ImportControlLookupResult, ControlSyncStatus
from foreign_tariff import (
    SYNC_ENABLED as FOREIGN_TARIFF_SYNC_ENABLED,
    UK_MEASURES_ARCHIVE_ENABLED,
    ForeignTariffEngine,
)
from ebti_decisions import SYNC_ENABLED as EBTI_SYNC_ENABLED, EbtiDecisionEngine
from access2markets import A2M_FILL_ENABLED, Access2MarketsEngine
from resmi_gazete import (
    INTEREST_RULES as GAZETTE_INTEREST_RULES,
    SYNC_ENABLED as RESMI_GAZETE_SYNC_ENABLED,
    ResmiGazeteArchive,
)
from background_jobs import registry as job_registry
from eu_taric import EU_TARIC_FILL_ENABLED, EuTaricEngine
from change_ledger import ChangeLedger
from review_policy import ReviewService, policy_from_env
from storage import StorageService
from classification_evidence import (
    ClassificationEvidenceEngine,
    ClassificationEvidenceSearchResult,
    ClassificationEvidenceStatus,
)
from security_firewall import guard_data, guard_text, redact_data

# Semantic search (optional; GEMINI_API_KEY or OPENROUTER_API_KEY, see EMBEDDING_PROVIDER)
from semantic_search import build_embedder, prepare_document_text, VectorStore, MevzuatProcessor, EmbeddingCache
from hybrid_index import HybridIndex, REFRESH_SECONDS as HYBRID_INDEX_REFRESH_SECONDS
import hybrid_corpora

_embedder = build_embedder()
SEMANTIC_SEARCH_AVAILABLE = _embedder is not None
if SEMANTIC_SEARCH_AVAILABLE:
    _processor = MevzuatProcessor()
    _embedding_cache = EmbeddingCache(ttl=3600)

# Simple logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ticaret_client = TicaretApiClient()
tariff_engine = TariffEngine()
exchange_rate_service = ExchangeRateService()
trade_measure_engine = TradeMeasureEngine()
excise_tax_index = ExciseTaxIndex()
vat_rate_index = VatRateIndex()
tariff_engine.trade_measures = trade_measure_engine
tariff_engine.excise_tax = excise_tax_index
tariff_engine.vat_rates = vat_rate_index
eylemio_client = EylemioClient()
control_engine = ImportControlEngine()
classification_engine = ClassificationEvidenceEngine()
# Yurt dışı tarife karşılaştırma (PRD Faz 4): UK açık API'si + AB/İsviçre resmî bağlantıları.
foreign_tariff_engine = ForeignTariffEngine()
# AB Bağlayıcı Tarife Bilgisi kararları (resmî günlük yayın akışı).
ebti_engine = EbtiDecisionEngine()
# AB TARIC oranları: sorgu başına ücretli dış kaynak; talep üzerine ve tavanlı toplu dolumla.
eu_taric_engine = EuTaricEngine(
    # Aday AB kodları Türk tarife cetvelinden türetilir: GTİP'in ilk 10 hanesi AB TARIC kodudur.
    code_source=lambda: tariff_engine.distinct_gtip_codes(width=10),
)
# Aynı AB verisinin ücretsiz kaynağı (Access2Markets açık uçları). Ücretli yolun yerine
# geçmez; yanında durur ve ``/api/admin/eu-taric/compare`` ikisini karşılaştırır.
access2markets_engine = Access2MarketsEngine(
    code_source=lambda: tariff_engine.distinct_gtip_codes(width=10),
)
# Resmî Gazete geçmiş arşivi: mevzuat metninin kalıcı, tarihli ve checksum'lı kopyası.
# Tarife/önlem verisi zaten sürümlü; arşivlenmeyen tek şey metnin kendisiydi.
resmi_gazete_archive = ResmiGazeteArchive()
# Unified, persistent change ledger shared by every official data engine.
change_ledger = ChangeLedger()
tariff_engine.ledger = change_ledger
control_engine.ledger = change_ledger
classification_engine.ledger = change_ledger
foreign_tariff_engine.ledger = change_ledger
ebti_engine.ledger = change_ledger
trade_measure_engine.store.ledger = change_ledger
# Editorial review gate (DATA_REVIEW_MODE=off|auto|strict); shared by the engines and the admin API.
review_policy = policy_from_env()
tariff_engine.review_policy = review_policy
control_engine.review_policy = review_policy
classification_engine.review_policy = review_policy
foreign_tariff_engine.review_policy = review_policy
ebti_engine.review_policy = review_policy
review_service = ReviewService(
    policy=review_policy,
    engines={
        "tariff": tariff_engine,
        "controls": control_engine,
        "classification": classification_engine,
        "foreign_tariff": foreign_tariff_engine,
        "ebti": ebti_engine,
    },
    ledger=change_ledger,
)
customs_advisor_service = CustomsAdvisor(
    tariff_engine=tariff_engine,
    control_engine=control_engine,
    classification_engine=classification_engine,
)
# Veri diskinin doluluğu ve yeri doldurulamaz veritabanlarının dönüşümlü yedeği.
storage_service = StorageService()
# Tool-calling assistant (PRD Faz 3.3): the LLM orchestrates these deterministic engines only.
customs_assistant = CustomsAssistant(
    tools=build_assistant_tools(
        tariff_engine=tariff_engine,
        control_engine=control_engine,
        classification_engine=classification_engine,
        trade_measure_engine=trade_measure_engine,
        excise_tax_index=excise_tax_index,
        exchange_rate_service=exchange_rate_service,
        vat_rate_index=vat_rate_index,
    )
)


# Extra background coroutines registered by the web layer (e.g. watch-list notifier).
BACKGROUND_LOOPS: list[tuple[str, "Callable[[], Coroutine[Any, Any, None]]"]] = []


def _register_loop(name: str, factory, *, enabled: bool = True, reason: str = "") -> None:
    """Döngüyü hem başlatma listesine hem kütüğe yaz.

    Kapalı iş de kütüğe **sebebiyle** girer: eskiden bayrağı kapalı olan iş hiçbir
    ekranda görünmüyordu ve "bu dolum neden ilerlemiyor" sorusunun cevabı yoktu.
    """
    job_registry.declare(name, enabled=enabled, reason=reason)
    if enabled:
        BACKGROUND_LOOPS.append((name, factory))


_register_loop("trade-measures-sync", trade_measure_engine.periodic_sync_loop)
_register_loop("vat-lists-sync", vat_rate_index.periodic_sync_loop)
# Resmî Gazete arşivi bugünden geriye kademeli dolar: her tur birkaç gün, her gün için
# normal sayı ve mükerrerleri. Kaynağa saygı sınırı istek arası beklemeyle sağlanır.
_register_loop(
    "resmi-gazete-archive",
    resmi_gazete_archive.periodic_sync_loop,
    enabled=RESMI_GAZETE_SYNC_ENABLED,
    reason="RESMI_GAZETE_SYNC_ENABLED kapalı — arşiv olduğu yerde durur, silinmez.",
)
_register_loop(
    "foreign-tariff-sync",
    foreign_tariff_engine.periodic_sync_loop,
    enabled=FOREIGN_TARIFF_SYNC_ENABLED,
    reason="FOREIGN_TARIFF_SYNC_ENABLED kapalı.",
)
_register_loop(
    "ebti-sync",
    ebti_engine.periodic_sync_loop,
    enabled=EBTI_SYNC_ENABLED,
    reason="EBTI_SYNC_ENABLED kapalı.",
)
# BK oranları yalnız emtia başına yayımlanıyor; arşiv kaynağı yormadan kademeli dolar.
_register_loop(
    "uk-measures-archive",
    foreign_tariff_engine.measures_archive_loop,
    enabled=FOREIGN_TARIFF_SYNC_ENABLED and UK_MEASURES_ARCHIVE_ENABLED,
    reason="FOREIGN_TARIFF_SYNC_ENABLED veya UK_MEASURES_ARCHIVE_ENABLED kapalı.",
)
# AB oranları kod × ülke başına ücretlidir; döngü aylık harcama tavanına kadar ilerler.
_register_loop(
    "eu-taric-fill",
    eu_taric_engine.fill_loop,
    enabled=EU_TARIC_FILL_ENABLED,
    reason="EU_TARIC_FILL_ENABLED kapalı — ücretli toplu dolum durdurulmuş durumda. "
    "Arşiv korunur; açıldığında kaldığı yerden devam eder.",
)
# Aynı katalog, ücretsiz kaynaktan: tavan yok, yalnız kaynağa saygı için hız sınırı var.
_register_loop(
    "access2markets-fill",
    access2markets_engine.periodic_fill_loop,
    enabled=A2M_FILL_ENABLED,
    reason="A2M_FILL_ENABLED kapalı — sınırlı kota kullanıcı sorgularına ayrıldı.",
)
# Yeri doldurulamaz veritabanları (hesaplar, ücretli AB TARIC arşivi, defter,
# geçmiş anlık görüntüler) günlük olarak aynı diskte yedeklenir.
_register_loop(
    "storage-backup",
    storage_service.periodic_backup_loop,
    enabled=storage_service.enabled,
    reason="Yedekleme kapalı (STORAGE_BACKUP_ENABLED).",
)


async def backfill_change_ledger() -> None:
    """One-off at startup: import historical snapshot transitions into the ledger."""
    await asyncio.sleep(20)
    try:
        counts = await asyncio.to_thread(
            lambda: {
                "tariff": tariff_engine.backfill_ledger(),
                "controls": control_engine.backfill_ledger(),
                "classification": classification_engine.backfill_ledger(),
                "trade_measures": trade_measure_engine.store.backfill_ledger(),
                "measure_rows": trade_measure_engine.store.ensure_measure_rows(),
                # Temporal validity: close open intervals of superseded snapshots (PRD Faz 1.4).
                "tariff_validity": tariff_engine.backfill_validity(),
                "control_validity": control_engine.backfill_validity(),
                "foreign_tariff": foreign_tariff_engine.backfill_ledger(),
                "ebti": ebti_engine.backfill_ledger(),
            }
        )
        if any(counts.values()):
            logger.info("Change ledger backfill: %s", counts)
    except Exception:  # noqa: BLE001
        logger.exception("Change ledger backfill failed")


_register_loop("change-ledger-backfill", backfill_change_ledger)

# Persistent hybrid search index (BM25 + embedding; PRD Faz 3.1).
hybrid_index = HybridIndex(embedder=_embedder)
# PRD Faz 3.2: sınıflandırma ve ön değerlendirme dipnotlu hibrit kanıt kullanır.
customs_advisor_service.hybrid_index = hybrid_index
# PRD Faz 4: AB BTB kararları sınıflandırma kanıtına girer.
customs_advisor_service.ebti_engine = ebti_engine
# İhracat yönü: hedef ülke oranı yalnız bu iki resmî motordan okunur. AB tarafı
# ön değerlendirmede archive_only ile çağrılır, yani ücretli aktör tetiklenmez.
customs_advisor_service.eu_taric_engine = eu_taric_engine
# AB dalında önce ücretsiz kaynak denenir; ücretli arşiv yedekte kalır.
customs_advisor_service.access2markets_engine = access2markets_engine
customs_advisor_service.foreign_tariff_engine = foreign_tariff_engine
# AB-27 KDV oranları (tohum + TEDB'den otomatik yükseltme).
eu_vat_index = EuVatRates()
customs_advisor_service.eu_vat_index = eu_vat_index
_register_loop("eu-vat-sync", eu_vat_index.periodic_sync_loop)


async def hybrid_index_refresh_loop() -> None:
    """Re-feed the hybrid index from every official corpus; only changed documents are re-embedded."""
    await asyncio.sleep(60)
    while True:
        try:
            corpora = await asyncio.to_thread(
                hybrid_corpora.collect_all,
                control_engine=control_engine,
                classification_engine=classification_engine,
                trade_measure_engine=trade_measure_engine,
                excise_index=excise_tax_index,
                vat_index=vat_rate_index,
                tariff_engine=tariff_engine,
                foreign_tariff_engine=foreign_tariff_engine,
                ebti_engine=ebti_engine,
                # Geçmiş sürümler de beslenir: motor veritabanlarında zaten duran arşiv
                # böylece arama katmanında da görünür ve `as_of` sorgulanabilir olur.
                include_history=True,
            )
            counts = await hybrid_index.refresh(corpora)
            logger.info("Hybrid index refresh: %s", counts)
        except Exception:  # noqa: BLE001
            logger.exception("Hybrid index refresh failed")
        await asyncio.sleep(HYBRID_INDEX_REFRESH_SECONDS)


_register_loop("hybrid-index-refresh", hybrid_index_refresh_loop)


@asynccontextmanager
async def _server_lifespan(server):
    """Keep the Ministry catalogue fresh without delaying ASGI startup."""
    extra_tasks = []
    for name, factory in BACKGROUND_LOOPS:
        task = asyncio.create_task(factory(), name=name)
        # Kütük, sessizce ölen bir döngüyü görünür kılar: görev istisnayla biterse
        # süreç çalışmaya devam eder, yalnız o iş durur.
        job_registry.track(name, task)
        extra_tasks.append(task)
    fixed = (
        ("ticaret-catalog-refresh", ticaret_client.periodic_refresh_loop),
        ("official-tariff-refresh", tariff_engine.periodic_sync_loop),
        ("official-import-controls-refresh", control_engine.periodic_sync_loop),
        ("official-classification-evidence-refresh", classification_engine.periodic_sync_loop),
    )
    fixed_tasks = []
    for name, factory in fixed:
        job_registry.declare(name)
        task = asyncio.create_task(factory(), name=name)
        job_registry.track(name, task)
        fixed_tasks.append(task)
    refresh_task, tariff_task, control_task, classification_task = fixed_tasks
    try:
        yield {
            "ticaret_client": ticaret_client,
            "customs_advisor": customs_advisor_service,
            "tariff_engine": tariff_engine,
            "control_engine": control_engine,
            "classification_engine": classification_engine,
            "hybrid_index": hybrid_index,
        }
    finally:
        for task in (refresh_task, tariff_task, control_task, classification_task, *extra_tasks):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await ticaret_client.close()
        await customs_advisor_service.close()
        await tariff_engine.close()
        await control_engine.close()
        await classification_engine.close()

app = FastMCP(
    name="MevzuatGovTrMCP",
    version="1.8.0",
    lifespan=_server_lifespan,
    instructions="MCP server for Turkish legislation search and content retrieval. "
    "Three source families: mevzuat.gov.tr, bedesten.adalet.gov.tr, and a continuously refreshed official "
    "Ministry of Trade catalogue covering customs, imports, exports, state supports, internal trade, consumer law, "
    "free zones, product safety, service trade, statistics/data, publications, country-market reports, commercial "
    "counsellor/attaché reports and contact information. "
    "\n\n"
    "== mevzuat.gov.tr tools (21 tools) ==\n"
    "9 legislation types: Kanun, KHK, Tüzük, Kurum Yönetmeliği, Tebliğ, CB Kararnamesi, CB Kararı, CB Yönetmeliği, CB Genelgesi. "
    "Each type has search and search_within tools. search_within supports keyword (AND/OR/NOT) and semantic search (GEMINI_API_KEY or OPENROUTER_API_KEY). "
    "IMPORTANT: These search tools are keyword-based (not by law number) - use 'katma değer vergisi' not '3065'. "
    "\n\n"
    "== bedesten.adalet.gov.tr tools (5 tools) ==\n"
    "Alternative API, no auth needed, supports 12 legislation types and Solr/Lucene search operators. "
    "Tools: search_mevzuat (unified search with type filter, supports law number search), "
    "get_mevzuat_content (full text), search_within_mevzuat (article keyword search), "
    "get_mevzuat_gerekce (law rationale/gerekçe), get_mevzuat_madde_tree (article tree/TOC). "
    "Solr operators: \"exact\", +required, -prohibited, wildcard*, fuzzy~, \"proximity\"~N, boost^N. "
    "NOTE: AND/OR/NOT do NOT work in search_mevzuat - use +term1 +term2 instead. "
    "\n\n"
    "== Ticaret Bakanlığı, Gümrükçe, resmî tarife ve ithalat kontrolü tools (18 tools) ==\n"
    "Use list_ticaret_sources to see source coverage and content kinds. Use search_ticaret_catalog for current "
    "Ministry metadata, get_ticaret_document for bounded full text, search_ticaret_content for bounded multi-document "
    "full-text search, and get_ticaret_catalog_status for freshness. Results always include official source URLs. "
    "For photo intake call describe_product_image first, confirm the visible attributes with the user, then call "
    "suggest_candidate_tariff_codes with the approved textual attributes and walk resolve_turkish_tariff_tree "
    "branch by branch until the user picks a GTIP12 line. "
    "For legal analysis, distinguish current and repealed material, cite the exact official source, state uncertainty, "
    "and treat the output as informational rather than a substitute for professional legal advice. "
    "Use prepare_customs_precheck before answering product-specific import questions. It returns a dated official "
    "evidence pack, missing intake fields, a user-rate-only cost calculation and a mandatory legal notice. Never "
    "present a photo-derived code as a binding GTIP or invent an uncited tax rate."
)

# Initialize client with caching enabled (1 hour TTL by default)
# Mistral API key will be loaded from environment variable MISTRAL_API_KEY
mevzuat_client = MevzuatApiClientNew(cache_ttl=3600, enable_cache=True)

# Tertip fallback order: try requested tertip first, then alternatives
TERTIP_FALLBACK_ORDER = ["5", "3"]


async def _get_content_with_tertip_fallback(
    mevzuat_no: str,
    mevzuat_tur: int,
    mevzuat_tertip: str = "5",
    resmi_gazete_tarihi: Optional[str] = None,
) -> MevzuatArticleContent:
    """Try to get content with the given tertip, falling back to alternatives if empty."""
    tertips_to_try = [mevzuat_tertip] + [t for t in TERTIP_FALLBACK_ORDER if t != mevzuat_tertip]

    for tertip in tertips_to_try:
        result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=mevzuat_tur,
            mevzuat_tertip=tertip,
            resmi_gazete_tarihi=resmi_gazete_tarihi,
        )
        if result.markdown_content and not result.error_message:
            if tertip != mevzuat_tertip:
                logger.info(f"Tertip fallback: {mevzuat_no} found with tertip {tertip} (requested {mevzuat_tertip})")
            return result

    # Return last result (with error) if all failed
    return result


# ============================================================================
# Shared semantic search helper
# ============================================================================

async def _semantic_search_within(
    mevzuat_no: str,
    query: str,
    mevzuat_tur: int,
    mevzuat_tertip: str = "5",
    max_results: int = 10,
    threshold: float = 0.3,
    resmi_gazete_tarihi: Optional[str] = None,
) -> str:
    """Shared helper for semantic search within any legislation type."""
    query = guard_text(query, source="semantik mevzuat araması", max_chars=4_000)
    # 1. Get content with tertip fallback (already cached by mevzuat_client)
    content_result = await _get_content_with_tertip_fallback(
        mevzuat_no=mevzuat_no,
        mevzuat_tur=mevzuat_tur,
        mevzuat_tertip=mevzuat_tertip,
        resmi_gazete_tarihi=resmi_gazete_tarihi,
    )

    if content_result.error_message:
        return f"Error fetching content: {content_result.error_message}"

    if not content_result.markdown_content:
        return f"Error: No content found for mevzuat {mevzuat_no}"

    content = content_result.markdown_content

    # 2. Check embedding cache
    cached = _embedding_cache.get(mevzuat_tur, mevzuat_tertip, mevzuat_no, content)
    if cached:
        vector_store, chunks = cached
    else:
        # 3. Process into chunks
        chunks = _processor.process_legislation(content, mevzuat_no, mevzuat_tur)
        if not chunks:
            return f"Error: Could not split content into searchable segments for mevzuat {mevzuat_no}"

        # 4. Encode documents (untrusted content is sanitised and redacted first)
        texts = [c.text for c in chunks]
        titles = [c.title for c in chunks]
        prepared = [prepare_document_text(text, title) for text, title in zip(texts, titles)]
        embeddings = np.asarray(await _embedder.embed(prepared, task="document"), dtype=np.float32)

        # 5. Build vector store
        vector_store = VectorStore(dimension=_embedder.dim)
        vector_store.add_documents(
            ids=[c.chunk_id for c in chunks],
            texts=texts,
            embeddings=embeddings,
            metadata=[c.metadata for c in chunks],
        )

        # 6. Cache
        _embedding_cache.put(mevzuat_tur, mevzuat_tertip, mevzuat_no, content, vector_store, chunks)

    # 7. Search
    query_embedding = np.asarray((await _embedder.embed([query], task="query"))[0], dtype=np.float32)
    results = vector_store.search(query_embedding, top_k=max_results, threshold=threshold)

    if not results:
        return f"No semantically similar content found for '{query}' in mevzuat {mevzuat_no}"

    # 8. Format results
    # Determine method description
    chunk_type = chunks[0].metadata.get('type', 'chunk') if chunks else 'chunk'
    method = "Article-based semantic search" if chunk_type == 'article' else "Chunk-based semantic search"

    output = []
    output.append("Semantic Search Results")
    output.append(f"Query: \"{query}\"")
    output.append(f"Legislation: {mevzuat_no} (type: {mevzuat_tur})")
    output.append(f"Method: {method} | Results: {len(results)}")
    output.append("")

    for doc, score in results:
        if chunk_type == 'article':
            madde_no = doc.metadata.get('madde_no', '?')
            madde_title = doc.metadata.get('madde_title', '')
            output.append(f"=== MADDE {madde_no} === (Similarity: {score:.2f})")
            if madde_title:
                output.append(f"Title: {madde_title}")
        else:
            chunk_idx = doc.metadata.get('chunk_index', 0)
            total = doc.metadata.get('total_chunks', 0)
            output.append(f"=== Chunk {chunk_idx + 1}/{total} === (Similarity: {score:.2f})")

        output.append("")
        output.append(doc.text)
        output.append("")

    return "\n".join(output)


async def _keyword_search_chunks(
    content: str,
    keyword: str,
    mevzuat_no: str,
    mevzuat_tur: int,
    case_sensitive: bool = False,
    max_results: int = 25,
) -> str:
    """Keyword search for chunk-based content (no article structure)."""
    # Try article split first for Teblig
    if mevzuat_tur == 9:
        from article_search import split_into_articles as _split
        articles = _split(content)
        if articles:
            matches = search_articles_by_keyword(content, keyword, case_sensitive, max_results)
            if matches:
                result = ArticleSearchResult(
                    mevzuat_no=mevzuat_no, mevzuat_tur=mevzuat_tur,
                    keyword=keyword, total_matches=len(matches), matching_articles=matches
                )
                return format_search_results(result)

    # Chunk-based keyword search
    from semantic_search.processor import MevzuatProcessor as _MevzuatProcessor
    processor = _processor if SEMANTIC_SEARCH_AVAILABLE else _MevzuatProcessor()
    chunks = processor.process_legislation(content, mevzuat_no, mevzuat_tur)

    if not chunks:
        return f"Error: Could not split content into searchable segments for mevzuat {mevzuat_no}"

    scored_chunks = []
    for chunk in chunks:
        matches, score = _matches_query(chunk.text, keyword, case_sensitive)
        if matches and score > 0:
            scored_chunks.append((chunk, score))

    scored_chunks.sort(key=lambda x: x[1], reverse=True)
    scored_chunks = scored_chunks[:max_results]

    if not scored_chunks:
        return f"No matches found for '{keyword}' in mevzuat {mevzuat_no}"

    output = []
    output.append(f"Keyword: '{keyword}'")
    output.append(f"Total matching segments: {len(scored_chunks)}")
    output.append("")

    for chunk, score in scored_chunks:
        chunk_type = chunk.metadata.get('type', 'chunk')
        if chunk_type == 'article':
            madde_no = chunk.metadata.get('madde_no', '?')
            output.append(f"=== MADDE {madde_no} ===")
        else:
            chunk_idx = chunk.metadata.get('chunk_index', 0)
            total = chunk.metadata.get('total_chunks', 0)
            output.append(f"=== Chunk {chunk_idx + 1}/{total} ===")
        output.append(f"Matches: {score}")
        output.append("")
        output.append("Full content:")
        output.append(chunk.text)
        output.append("")

    return "\n".join(output)


@app.tool()
async def search_kanun(
    aranacak_ifade: str = Field(
        ...,
        description='Search query with optional Boolean operators: simple word (yatırımcı), AND (yatırımcı AND tazmin), OR (vergi OR ücret), NOT (yatırımcı NOT kurum), + for required (+term), grouping with (), exact phrase with quotes ("mali sıkıntı")'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases like 'mali sıkıntı'."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start date for filtering results (format: DD.MM.YYYY, e.g., '01.01.2020')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End date for filtering results (format: DD.MM.YYYY, e.g., '31.12.2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish laws (Kanun) in both titles and content on mevzuat.gov.tr.

    IMPORTANT: Search is keyword-based, NOT by law number. Use descriptive Turkish terms.
    - WRONG: "3065" or "6362" (numbers won't find laws reliably)
    - RIGHT: "katma değer vergisi" (finds KDV Kanunu No. 3065)
    - RIGHT: "sermaye piyasası" (finds Sermaye Piyasası Kanunu No. 6362)
    - RIGHT: "gümrük kanunu" (finds Gümrük Kanunu No. 4458)
    - RIGHT: "gelir vergisi" (finds Gelir Vergisi Kanunu No. 193)
    - RIGHT: "ceza muhakemesi" (finds CMK No. 5271)
    - RIGHT: "vergi usul" (finds VUK No. 213)

    Use 'search_within_kanun' to search within a specific law's articles after finding its number.

    Query Syntax:
    - Simple keyword: yatırımcı
    - Boolean AND: yatırımcı AND tazmin (both terms required)
    - Boolean OR: yatırımcı OR müşteri (at least one term)
    - Boolean NOT: yatırımcı NOT kurum (first yes, second no)
    - Required term: +yatırımcı +tazmin (similar to AND)
    - Grouping: (yatırımcı OR müşteri) AND tazmin
    - Exact phrase: "mali sıkıntı" (or use tam_cumle=true)

    Returns: Law number, title, acceptance date, Official Gazette date and issue number.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Kanun",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_kanun' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No legislation found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_mevzuat'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_kanun(
    mevzuat_no: str = Field(
        ...,
        description="The legislation number to search within (e.g., '6362', '5237')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Legislation series from search results (e.g., '3', '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific law's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "yatırımcı AND tazmin", '"mali sıkıntı"', "vergi OR ücret"
    Semantic examples: "yatırımcının zararının tazmini", "sermaye piyasası düzenlemeleri"
    """
    logger.info(f"Tool 'search_within_kanun' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=1,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        # Keyword search
        content_result = await _get_content_with_tertip_fallback(
            mevzuat_no=mevzuat_no, mevzuat_tur=1, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching legislation content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=1, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in Kanun {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_kanun' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_teblig(
    aranacak_ifade: str = Field(
        ...,
        description='Search query with optional Boolean operators: simple word (vergi), AND (vergi AND muafiyet), OR (muafiyet OR istisna), NOT (vergi NOT gelir), + for required (+term), grouping with (), exact phrase with quotes ("katma değer vergisi")'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2020')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish communiqués (Tebliğ) in both titles and content on mevzuat.gov.tr.

    IMPORTANT: Search is keyword-based, NOT by number. Use descriptive Turkish terms.
    Communiqués are regulatory documents issued by various government institutions.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "katma değer vergisi" - Find VAT-related communiqués
    - "muafiyet OR istisna" - Communiqués about exemptions
    - "gümrük" - Customs-related communiqués
    - "ithalat" or "ihracat" - Import/export communiqués

    Returns: Communiqué number, title, publication date, Official Gazette info.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Tebliğ",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_teblig' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No communiqués found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_teblig'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def get_teblig_content(
    mevzuat_no: str = Field(
        ...,
        description="The communiqué number from search results (e.g., '42331')"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Communiqué series from search results (e.g., '5')"
    )
) -> MevzuatArticleContent:
    """
    Retrieve the full content of a Turkish communiqué (Tebliğ) in Markdown format.

    This tool fetches the complete text of a communiqué identified by its number.
    Use 'search_teblig' first to find the communiqué number and series.

    Returns:
    - Full communiqué content formatted as Markdown
    - Ready for analysis, summarization, or question answering

    Example usage:
    1. Search for communiqués: search_teblig(aranacak_ifade="katma değer vergisi")
    2. Get full content: get_teblig_content(mevzuat_no="42331", mevzuat_tertip="5")
    """
    logger.info(f"Tool 'get_teblig_content' called: {mevzuat_no}, tertip: {mevzuat_tertip}")

    try:
        result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=9,  # Tebliğ
            mevzuat_tertip=mevzuat_tertip
        )

        if result.error_message:
            logger.warning(f"Error fetching communiqué content: {result.error_message}")

        return result

    except Exception as e:
        logger.exception(f"Error in tool 'get_teblig_content' for {mevzuat_no}")
        return MevzuatArticleContent(
            madde_id=mevzuat_no,
            mevzuat_id=mevzuat_no,
            markdown_content="",
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_cbk(
    aranacak_ifade: str = Field(
        ...,
        description='Search query with optional Boolean operators: simple word (organize), AND (organize AND suç), OR (suç OR ceza), NOT (organize NOT terör), + for required (+term), grouping with (), exact phrase with quotes ("organize suç")'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Decrees (Cumhurbaşkanlığı Kararnamesi) in both titles and content.

    IMPORTANT: Search is keyword-based, NOT by decree number. Use descriptive Turkish terms.
    Presidential Decrees are executive orders issued by the President of Turkey (post-2017).

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "organize suç" - Find decrees about organized crime
    - "kamu OR devlet" - Decrees about public or state matters
    - "bakanlık AND teşkilat" - Ministry organization decrees

    Returns: Decree number, title, publication date, Official Gazette info.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Cumhurbaşkanlığı Kararnamesi",
        aranacak_ifade=aranacak_ifade,
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_cbk' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No Presidential Decrees found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbk'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_cbk(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Decree number to search within (e.g., '1', '32')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Decree series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Decree's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "organize AND suç", '"organize suç"', "devlet OR kamu"
    Semantic examples: "organize suç örgütleri ile mücadele", "bakanlık teşkilat yapısı"
    """
    logger.info(f"Tool 'search_within_cbk' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=19,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=19, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching decree content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=19, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in CBK {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbk' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_cbyonetmelik(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with optional Boolean operators: simple word (yatırımcı), AND (yatırımcı AND tazmin), OR (vergi OR ücret), NOT (yatırımcı NOT kurum), + for required (+term), grouping with (), exact phrase with quotes ("mali sıkıntı"). Leave empty to list all regulations.'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number (1-indexed)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (max 100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Regulations (Cumhurbaşkanlığı Yönetmeliği / CB Yönetmeliği) in both titles and content.

    IMPORTANT: Search is keyword-based, NOT by number. Use descriptive Turkish terms.
    These are regulations issued directly by the Presidency. For institutional regulations (Kurum Yönetmeliği),
    use 'search_kurum_yonetmelik' instead.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "ihale" - Procurement regulations
    - "taşınır AND mal" - Movable property regulations
    - "kamu ihale" - Public procurement regulations
    - Leave empty to list all, use date filters for period

    Returns: Regulation number, title, publication date, Official Gazette info.
    """
    logger.info(f"Tool 'search_cbyonetmelik' called with query: {aranacak_ifade}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="CB Yönetmeliği",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Search completed: {result.total_results} total results")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbyonetmelik'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"aranacak_ifade": aranacak_ifade},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_cbyonetmelik(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Regulation number to search within (e.g., '10453', '9014')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Regulation series from search results (typically '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case (false = case-insensitive, default). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Regulation's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "taşınır AND mal", '"ihale kanunu"', "kamu OR devlet"
    Semantic examples: "taşınır mal yönetimi ve zimmet işlemleri", "kamu ihale süreçleri"
    """
    logger.info(f"Tool 'search_within_cbyonetmelik' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=21,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=21, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for regulation {mevzuat_no}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        if not matches:
            return f"No articles found matching '{keyword}' in regulation {mevzuat_no}"

        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=21, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbyonetmelik' for regulation {mevzuat_no}")
        return f"Error: An unexpected error occurred: {str(e)}"


@app.tool()
async def search_cbbaskankarar(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with optional Boolean operators: simple word (organize), AND (organize AND suç), OR (suç OR ceza), NOT (organize NOT terör), + for required (+term), grouping with (), exact phrase with quotes ("organize suç"). Leave empty to list all decrees.'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number for pagination (starts at 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Decisions (Cumhurbaşkanı Kararı) in both titles and content.

    IMPORTANT: Search is keyword-based, NOT by decision number. Use descriptive Turkish terms.
    Presidential Decisions are executive decisions (different from Presidential Decrees/Kararnamesi).
    Note: Bakanlar Kurulu Kararı (BKK) is NOT a separate type - older BKKs may appear here or in Kanun.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "atama" - Find decisions about appointments
    - "ihracat AND rejim" - Export regime decisions
    - "vergi" or "gümrük" - Tax or customs decisions
    - Leave empty with dates to list all decisions from a period

    Returns: Decision number, title, publication date, Official Gazette info. PDF format only.
    """
    search_req = MevzuatSearchRequestNew(
        mevzuat_tur="Cumhurbaşkanı Kararı",
        aranacak_ifade=aranacak_ifade or "",
        aranacak_yer=aranacak_yer,
        tam_cumle=tam_cumle,
        mevzuat_no=None,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_number=page_number,
        page_size=page_size
    )

    log_params = search_req.model_dump(exclude_defaults=True)
    logger.info(f"Tool 'search_cbbaskankarar' called with parameters: {log_params}")

    try:
        result = await mevzuat_client.search_documents(search_req)

        if not result.documents and not result.error_message:
            result.error_message = "No Presidential Decisions found matching the specified criteria."

        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbbaskankarar'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used=log_params,
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def get_cbbaskankarar_content(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Decision number from search results (e.g., '10452')"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Decision series from search results (e.g., '5')"
    )
) -> MevzuatArticleContent:
    """
    Retrieve the full content of a Turkish Presidential Decision (Cumhurbaşkanı Kararı) in Markdown format.

    This tool fetches the PDF document and converts it to Markdown.
    Presidential Decisions are available only as PDF files.
    Use 'search_cbbaskankarar' first to find the decision number and series.

    Returns:
    - Full decision content formatted as Markdown (converted from PDF)
    - Ready for analysis, summarization, or question answering

    Example usage:
    1. Search for decisions: search_cbbaskankarar(baslangic_tarihi="2023", bitis_tarihi="2024")
    2. Get full content: get_cbbaskankarar_content(mevzuat_no="10452", mevzuat_tertip="5")
    """
    logger.info(f"Tool 'get_cbbaskankarar_content' called: {mevzuat_no}, tertip: {mevzuat_tertip}")

    try:
        result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=20,  # Cumhurbaşkanı Kararı
            mevzuat_tertip=mevzuat_tertip
        )

        if result.error_message:
            logger.warning(f"Error fetching decision content: {result.error_message}")

        return result

    except Exception as e:
        logger.exception(f"Error in tool 'get_cbbaskankarar_content' for {mevzuat_no}")
        return MevzuatArticleContent(
            madde_id=mevzuat_no,
            mevzuat_id=mevzuat_no,
            markdown_content="",
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_cbgenelge(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with optional Boolean operators: simple word (organize), AND (organize AND suç), OR (suç OR ceza), NOT (organize NOT terör), + for required (+term), grouping with (), exact phrase with quotes ("organize suç"). Leave empty to list all circulars.'
    ),
    tam_cumle: bool = Field(
        False,
        description="Exact phrase match (true) or any word match (false, default). Set to true when searching for exact phrases."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2018')"
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2024')"
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number (1-indexed)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (max 100)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Presidential Circulars (Cumhurbaşkanlığı Genelgesi / CB Genelgesi) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by circular number. Use descriptive Turkish terms.
    Use 'get_cbgenelge_content' with mevzuat_no and resmi_gazete_tarihi to retrieve full PDF content.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "koordinasyon" - Coordination circulars
    - Leave empty with dates to list all circulars from a period

    Returns: Circular number, title, publication date, Official Gazette info. PDF format only.
    """
    logger.info(f"Tool 'search_cbgenelge' called with query: {aranacak_ifade}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="CB Genelgesi",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Search completed: {result.total_results} total results")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_cbgenelge'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"aranacak_ifade": aranacak_ifade},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def get_cbgenelge_content(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Circular number from search results (e.g., '16', '15')"
    ),
    resmi_gazete_tarihi: str = Field(
        ...,
        description="Official Gazette date from search results in DD/MM/YYYY format (e.g., '20/09/2025')"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Circular series from search results (e.g., '5')"
    )
) -> MevzuatArticleContent:
    """
    Retrieve the full content of a Turkish Presidential Circular (Cumhurbaşkanlığı Genelgesi) in Markdown format.

    This tool fetches the PDF document and converts it to Markdown.
    Presidential Circulars are available only as PDF files.
    Use 'search_cbgenelge' first to find the circular number and Official Gazette date.

    IMPORTANT: You must provide the 'resmi_gazete_tarihi' (Official Gazette date) from the search results.
    This is required to construct the correct PDF URL.

    Returns:
    - Full circular content formatted as Markdown (converted from PDF)
    - Ready for analysis, summarization, or question answering

    Example usage:
    1. Search for circulars: search_cbgenelge(baslangic_tarihi="2025")
    2. Get full content: get_cbgenelge_content(mevzuat_no="16", resmi_gazete_tarihi="20/09/2025", mevzuat_tertip="5")
    """
    logger.info(f"Tool 'get_cbgenelge_content' called: {mevzuat_no}, RG date: {resmi_gazete_tarihi}, tertip: {mevzuat_tertip}")

    try:
        result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no,
            mevzuat_tur=22,  # Cumhurbaşkanlığı Genelgesi
            mevzuat_tertip=mevzuat_tertip,
            resmi_gazete_tarihi=resmi_gazete_tarihi
        )

        if result.error_message:
            logger.warning(f"Error fetching circular content: {result.error_message}")

        return result

    except Exception as e:
        logger.exception(f"Error in tool 'get_cbgenelge_content' for {mevzuat_no}")
        return MevzuatArticleContent(
            madde_id=mevzuat_no,
            mevzuat_id=mevzuat_no,
            markdown_content="",
            error_message=f"An unexpected error occurred: {str(e)}"
        )


# ============================================================================
# KHK (Kanun Hükmünde Kararname) Tools
# ============================================================================

@app.tool()
async def search_khk(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with Boolean operators and wildcards. Examples: "değişiklik" (simple), "sağlık AND düzenleme" (AND), "bakanlık OR kurum" (OR), "kanun NOT yürürlük" (NOT), "değişiklik*" (wildcard), "güvenlik sistemi" (exact phrase with quotes). Leave empty to list all KHKs in date range.'
    ),
    tam_cumle: bool = Field(
        False,
        description="If True, searches for exact phrase match. If False (default), searches for any word match with Boolean operators."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2010'). Use with bitis_tarihi to define a date range."
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2018'). Use with baslangic_tarihi to define a date range."
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number of results to retrieve (starts from 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Decree Laws (Kanun Hükmünde Kararname / KHK) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by KHK number. Use descriptive Turkish terms.
    KHKs were abolished after the 2017 constitutional referendum (last issued 2018).
    Previously enacted KHKs remain in force unless repealed.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "sağlık AND düzenleme" - Health-related KHKs
    - "anayasa" - Constitutional KHKs
    - Leave empty with dates (e.g., 2010-2018) to list all KHKs from a period

    Returns: KHK number, title, dates, Official Gazette info.
    """
    logger.info(f"Tool 'search_khk' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="KHK",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Found {result.total_results} KHKs")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_khk'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"error": str(e)},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_khk(
    mevzuat_no: str = Field(
        ...,
        description="The KHK number to search within (e.g., '703', '700', '665')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="KHK series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Decree Law's (KHK) articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "kanun AND değişiklik", '"kanun hükmünde"', "bakanlık OR kurum"
    Semantic examples: "sağlık alanında yapılan düzenlemeler", "anayasa değişikliği"
    """
    logger.info(f"Tool 'search_within_khk' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=4,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=4, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching KHK content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=4, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in KHK {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_khk' for {mevzuat_no}")
        return f"An unexpected error occurred while searching KHK {mevzuat_no}: {str(e)}"


# ============================================================================
# Tüzük (Statute/Regulation) Tools
# ============================================================================

@app.tool()
async def search_tuzuk(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with Boolean operators and wildcards. Examples: "tapu" (simple), "sicil AND kayıt" (AND), "tescil OR ilan" (OR), "vakıf NOT kurul" (NOT), "tescil*" (wildcard), "medeni kanun" (exact phrase with quotes). Leave empty to list all statutes in date range.'
    ),
    tam_cumle: bool = Field(
        False,
        description="If True, searches for exact phrase match. If False (default), searches for any word match with Boolean operators."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2008'). Use with bitis_tarihi to define a date range."
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2013'). Use with baslangic_tarihi to define a date range."
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number of results to retrieve (starts from 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Turkish Statutes/Regulations (Tüzük) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by statute number. Use descriptive Turkish terms.
    Tüzük are regulatory statutes that implement and detail the provisions of laws.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "tapu" - Land registry related statutes
    - "vakıf AND tescil" - Foundation registration statutes
    - Leave empty with dates to list all statutes from a period

    Returns: Statute number, title, dates, Official Gazette info.
    """
    logger.info(f"Tool 'search_tuzuk' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="Tuzuk",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Found {result.total_results} statutes")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_tuzuk'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"error": str(e)},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_tuzuk(
    mevzuat_no: str = Field(
        ...,
        description="The statute number to search within (e.g., '20135150', '20134513', '200814001')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Statute series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Statute's (Tüzük) articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "tapu AND sicil", '"sicil kayıt"', "tescil OR ilan"
    Semantic examples: "tapu sicil kayıt işlemleri", "vakıf tescil süreci"
    """
    logger.info(f"Tool 'search_within_tuzuk' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=2,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=2, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching statute content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=2, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in Tüzük {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_tuzuk' for {mevzuat_no}")
        return f"An unexpected error occurred while searching Tüzük {mevzuat_no}: {str(e)}"


# ============================================================================
# Kurum ve Kuruluş Yönetmeliği Tools
# ============================================================================

@app.tool()
async def search_kurum_yonetmelik(
    aranacak_ifade: Optional[str] = Field(
        None,
        description='Search query with Boolean operators and wildcards. Examples: "nükleer" (simple), "ihracat AND kontrol" (AND), "denetim OR teftiş" (OR), "mali NOT ceza" (NOT), "kontrol*" (wildcard), "ithalat ihracat" (exact phrase with quotes). Leave empty to list all regulations in date range.'
    ),
    tam_cumle: bool = Field(
        False,
        description="If True, searches for exact phrase match. If False (default), searches for any word match with Boolean operators."
    ),
    baslangic_tarihi: Optional[str] = Field(
        None,
        description="Start year for filtering results (format: YYYY, e.g., '2020'). Use with bitis_tarihi to define a date range."
    ),
    bitis_tarihi: Optional[str] = Field(
        None,
        description="End year for filtering results (format: YYYY, e.g., '2025'). Use with baslangic_tarihi to define a date range."
    ),
    page_number: int = Field(
        1,
        ge=1,
        description="Page number of results to retrieve (starts from 1)"
    ),
    aranacak_yer: int = Field(
        3,
        ge=1,
        le=3,
        description="Where to search: 1=Title only, 2=Content only, 3=Both title and content (default)"
    ),
    page_size: int = Field(
        25,
        ge=1,
        le=100,
        description="Number of results per page (1-100, default: 25)"
    )
) -> MevzuatSearchResultNew:
    """
    Search for Institutional/Organizational Regulations (Kurum ve Kuruluş Yönetmeliği) in titles and content.

    IMPORTANT: Search is keyword-based, NOT by regulation number. Use descriptive Turkish terms.
    These are regulations issued by governmental institutions (ministries, agencies, boards).
    This is the largest dataset with 8686+ regulations. Use for: Gümrük Yönetmeliği, İthalat/İhracat
    Yönetmeliği, and similar institutional regulations.

    Query Syntax: Simple keyword, AND, OR, NOT, +required, (grouping), "exact phrase"

    Example queries:
    - "gümrük" - Customs regulations (e.g., Gümrük Yönetmeliği)
    - "ithalat" or "ihracat" - Import/export regulations
    - "nükleer" - Nuclear regulations
    - "adalet AND akademi" - Justice academy regulations
    - Leave empty with dates to list all regulations from a period

    Returns: Regulation number, title, dates, Official Gazette info.
    """
    logger.info(f"Tool 'search_kurum_yonetmelik' called: '{aranacak_ifade}', dates: {baslangic_tarihi}-{bitis_tarihi}")

    try:
        search_req = MevzuatSearchRequestNew(
            mevzuat_tur="Kurum Yönetmeliği",
            aranacak_ifade=aranacak_ifade or "",
            aranacak_yer=aranacak_yer,
            tam_cumle=tam_cumle,
            mevzuat_no=None,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_number=page_number,
            page_size=page_size
        )

        result = await mevzuat_client.search_documents(search_req)
        logger.info(f"Found {result.total_results} institutional regulations")
        return result

    except Exception as e:
        logger.exception("Error in tool 'search_kurum_yonetmelik'")
        return MevzuatSearchResultNew(
            documents=[],
            total_results=0,
            current_page=page_number,
            page_size=page_size,
            total_pages=0,
            query_used={"error": str(e)},
            error_message=f"An unexpected error occurred: {str(e)}"
        )


@app.tool()
async def search_within_kurum_yonetmelik(
    mevzuat_no: str = Field(
        ...,
        description="The regulation number to search within (e.g., '42641', '42638', '42613')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Regulation series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching articles to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Institutional Regulation's articles using keyword or semantic search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "nükleer AND ihracat", '"ihracat kontrol"', "denetim OR teftiş"
    Semantic examples: "nükleer madde ihracat kontrol düzenlemeleri", "disiplin cezaları"
    """
    logger.info(f"Tool 'search_within_kurum_yonetmelik' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=7,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=7, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching regulation content: {content_result.error_message}"

        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        result = ArticleSearchResult(
            mevzuat_no=mevzuat_no, mevzuat_tur=7, keyword=keyword,
            total_matches=len(matches), matching_articles=matches
        )
        if len(matches) == 0:
            return f"No articles found containing '{keyword}' in Kurum Yönetmeliği {mevzuat_no}"
        return format_search_results(result)

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_kurum_yonetmelik' for {mevzuat_no}")
        return f"An unexpected error occurred while searching Kurum Yönetmeliği {mevzuat_no}: {str(e)}"


# ============================================================================
# New search_within tools (Tebliğ, CB Kararı, CB Genelgesi)
# ============================================================================

@app.tool()
async def search_within_teblig(
    mevzuat_no: str = Field(
        ...,
        description="The communiqué number to search within (e.g., '42331')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Communiqué series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching segments to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific communiqué's (Tebliğ) content using keyword or semantic search.

    Tries article-based splitting first; if no articles found, falls back to chunk-based search.

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "vergi AND muafiyet", '"katma değer"', "istisna OR muafiyet"
    Semantic examples: "vergi muafiyeti koşulları", "KDV iade işlemleri"
    """
    logger.info(f"Tool 'search_within_teblig' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=9,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        # Keyword search
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=9, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching communiqué content: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for Tebliğ {mevzuat_no}"

        # Try article-based search first
        matches = search_articles_by_keyword(
            markdown_content=content_result.markdown_content,
            keyword=keyword, case_sensitive=case_sensitive, max_results=max_results
        )
        if matches:
            result = ArticleSearchResult(
                mevzuat_no=mevzuat_no, mevzuat_tur=9, keyword=keyword,
                total_matches=len(matches), matching_articles=matches
            )
            return format_search_results(result)

        # Fallback to chunk-based keyword search
        return await _keyword_search_chunks(
            content=content_result.markdown_content, keyword=keyword,
            mevzuat_no=mevzuat_no, mevzuat_tur=9,
            case_sensitive=case_sensitive, max_results=max_results
        )

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_teblig' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_within_cbbaskankarar(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Decision number to search within (e.g., '1733', '10452')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Decision series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching segments to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Decision's (CB Kararı) content using keyword or semantic search.

    Presidential Decisions are PDF-based and use chunk-based splitting (no article structure).

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "atama AND görev", '"ihracat rejimi"', "vergi OR gümrük"
    Semantic examples: "kamu personeli atama kararları", "ihracat rejimi düzenlemeleri"
    """
    logger.info(f"Tool 'search_within_cbbaskankarar' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=20,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results
            )

        # Keyword search (chunk-based)
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=20, mevzuat_tertip=mevzuat_tertip
        )
        if content_result.error_message:
            return f"Error fetching decision content: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for CB Kararı {mevzuat_no}"

        return await _keyword_search_chunks(
            content=content_result.markdown_content, keyword=keyword,
            mevzuat_no=mevzuat_no, mevzuat_tur=20,
            case_sensitive=case_sensitive, max_results=max_results
        )

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbbaskankarar' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_within_cbgenelge(
    mevzuat_no: str = Field(
        ...,
        description="The Presidential Circular number to search within (e.g., '16', '15')"
    ),
    keyword: str = Field(
        ...,
        description='Search query. For keyword mode: supports AND/OR/NOT operators (uppercase). For semantic mode: use natural language.'
    ),
    resmi_gazete_tarihi: str = Field(
        ...,
        description="Official Gazette date from search results in DD/MM/YYYY format (e.g., '20/09/2025') - REQUIRED for PDF retrieval"
    ),
    mevzuat_tertip: str = Field(
        "5",
        description="Circular series from search results (e.g., '5')"
    ),
    case_sensitive: bool = Field(
        False,
        description="Whether to match case when searching (default: False). Only used in keyword mode."
    ),
    max_results: int = Field(
        25,
        ge=1,
        le=50,
        description="Maximum number of matching segments to return (1-50, default: 25)"
    ),
    semantic: bool = Field(
        False,
        description="True: semantic search (natural language query, requires OPENROUTER_API_KEY). False: keyword search (Boolean operators AND/OR/NOT)."
    )
) -> str:
    """
    Search within a specific Presidential Circular's (CB Genelgesi) content using keyword or semantic search.

    Presidential Circulars are PDF-based and use chunk-based splitting (no article structure).
    IMPORTANT: resmi_gazete_tarihi is required (from search_cbgenelge results).

    Modes:
    - semantic=False (default): Keyword search with Boolean operators (AND/OR/NOT, uppercase required)
    - semantic=True: Natural language semantic search using AI embeddings (requires GEMINI_API_KEY or OPENROUTER_API_KEY)

    Keyword examples: "koordinasyon AND toplantı", '"kamu yönetimi"'
    Semantic examples: "bakanlıklar arası koordinasyon düzeni", "tasarruf tedbirleri"
    """
    logger.info(f"Tool 'search_within_cbgenelge' called: {mevzuat_no}, keyword: '{keyword}', semantic: {semantic}")

    try:
        if semantic:
            if not SEMANTIC_SEARCH_AVAILABLE:
                return "Error: Semantic search requires GEMINI_API_KEY or OPENROUTER_API_KEY (EMBEDDING_PROVIDER)."
            return await _semantic_search_within(
                mevzuat_no=mevzuat_no, query=keyword, mevzuat_tur=22,
                mevzuat_tertip=mevzuat_tertip, max_results=max_results,
                resmi_gazete_tarihi=resmi_gazete_tarihi
            )

        # Keyword search (chunk-based)
        content_result = await mevzuat_client.get_content(
            mevzuat_no=mevzuat_no, mevzuat_tur=22, mevzuat_tertip=mevzuat_tertip,
            resmi_gazete_tarihi=resmi_gazete_tarihi
        )
        if content_result.error_message:
            return f"Error fetching circular content: {content_result.error_message}"
        if not content_result.markdown_content:
            return f"Error: No content found for CB Genelgesi {mevzuat_no}"

        return await _keyword_search_chunks(
            content=content_result.markdown_content, keyword=keyword,
            mevzuat_no=mevzuat_no, mevzuat_tur=22,
            case_sensitive=case_sensitive, max_results=max_results
        )

    except Exception as e:
        logger.exception(f"Error in tool 'search_within_cbgenelge' for {mevzuat_no}")
        return f"An unexpected error occurred: {str(e)}"


# ============================================================================
# Bedesten API tools (bedesten.adalet.gov.tr - alternative, no auth needed)
# ============================================================================

from bedesten_client import BedestenClient, _strip_html
from bedesten_models import BedMaddeNode

bedesten_client = BedestenClient(cache_ttl=3600, enable_cache=True)

# Valid type codes for mevzuatTurList filter
_BED_VALID_TYPES = {
    "KANUN", "CB_KARARNAME", "YONETMELIK", "CB_YONETMELIK", "CB_KARAR",
    "CB_GENELGE", "KHK", "TUZUK", "KKY", "UY", "TEBLIGLER", "MULGA",
}


def _flatten_tree(nodes: list[BedMaddeNode]) -> list[BedMaddeNode]:
    """Flatten a nested tree of madde nodes into a flat list."""
    flat = []
    for node in nodes:
        flat.append(node)
        if node.children:
            flat.extend(_flatten_tree(node.children))
    return flat


def _format_tree(nodes: list[BedMaddeNode], indent: int = 0) -> str:
    """Format article tree as indented text."""
    lines = []
    for node in nodes:
        prefix = "  " * indent
        label = node.madde_baslik or ""
        if not label:
            no = str(node.madde_no) if node.madde_no is not None else ""
            title = node.title or node.description or ""
            label = f"{no}: {title}" if no and title else (no or title)
        mid = node.madde_id or ""
        gid = f" | gerekceId:{node.gerekce_id}" if node.gerekce_id else ""
        lines.append(f"{prefix}- {label} (maddeId:{mid}{gid})")
        if node.children:
            lines.append(_format_tree(node.children, indent + 1))
    return "\n".join(lines)


@app.tool()
async def search_mevzuat(
    phrase: str = Field(
        "",
        description=(
            "Full-text search in document content (Solr/Lucene syntax). "
            "Searches inside the legislation text, not just the title. "
            "Leave empty to browse/list or use mevzuat_adi for title search. "
            "Solr operators: \"exact phrase\", +required -prohibited, wildcard*, single?, fuzzy~, fuzzy~N, \"proximity\"~N, boost^N. "
            "NOTE: AND/OR/NOT do NOT work here - use +term1 +term2 instead of term1 AND term2, "
            "use -term instead of NOT term, use 'term1 term2' (space) instead of term1 OR term2. "
            "Examples: 'ticaret' (simple), '\"katma değer vergisi\"' (exact phrase), "
            "'+yatırımcı +tazmin' (both required), 'yatırımcı -kurum' (exclude), "
            "'yatırım*' (wildcard), '*ımcı' (leading wildcard), 'yatırımc?' (single char wildcard), "
            "'yatırımcı~' (fuzzy), 'yatırımcı~2' (fuzzy with distance), "
            "'\"yatırımcı tazmin\"~5' (proximity within 5 words), 'yatırımcı^2 tazmin' (boost first term)"
        ),
    ),
    mevzuat_adi: str = Field(
        "",
        description=(
            "Title/keyword search (Aranacak Kavram). Searches in legislation title/name. "
            "Use Turkish keywords, not law numbers. Multiple words are AND-matched (all must appear in title). "
            "Supports only: simple keywords, trailing wildcard (ticar*), single char wildcard (ticare?). "
            "For exact phrase match use tamCumle=True instead of quotes. "
            "Do NOT use quotes, +, -, ~, ^, or other Solr operators here (they break the search). "
            "Examples: 'ticaret kanunu', 'ceza', 'gümrük', 'sermaye piyasası', 'gelir vergisi', 'ticar*'. "
            "Can be used alone or together with phrase for combined filtering."
        ),
    ),
    mevzuat_no: Optional[str] = Field(
        None,
        description=(
            "Legislation number filter. Directly filters by the official number. "
            "E.g., '5237' for Türk Ceza Kanunu, '6102' for Türk Ticaret Kanunu, '6362' for Sermaye Piyasası Kanunu."
        ),
    ),
    mevzuat_tur: Optional[str] = Field(
        None,
        description=(
            "Filter by legislation type. Leave empty to search all types. "
            "Single type or comma-separated for multiple types. "
            "Types: KANUN (Kanunlar), CB_KARARNAME (Cumhurbaşkanı Kararnameleri), "
            "YONETMELIK (Bakanlar Kurulu Yönetmelikleri), CB_YONETMELIK (Cumhurbaşkanlığı Yönetmelikleri), "
            "CB_KARAR (Cumhurbaşkanı Kararları), CB_GENELGE (Cumhurbaşkanlığı Genelgeleri), "
            "KHK (Kanun Hükmünde Kararnameler), TUZUK (Tüzükler), "
            "KKY (Kurum ve Kuruluş Yönetmelikleri), UY (Üniversite Yönetmelikleri), "
            "TEBLIGLER (Tebliğler), MULGA (Mülga Mevzuat). "
            "Examples: 'KANUN', 'KANUN,KHK', 'TEBLIGLER,KKY'"
        ),
    ),
    basliktaAra: bool = Field(
        True,
        description=(
            "When True (default), mevzuat_adi searches only in legislation titles. "
            "When False, mevzuat_adi searches in both title and content."
        ),
    ),
    tamCumle: bool = Field(
        False,
        description=(
            "Exact phrase match for mevzuat_adi. "
            "When True, the entire mevzuat_adi text must appear as an exact phrase. "
            "When False (default), individual words are matched. "
            "Example: 'katma değer vergisi' with tamCumle=True finds only exact matches."
        ),
    ),
    resmi_gazete_tarihi_start: Optional[str] = Field(
        None,
        description=(
            "Start date filter for Official Gazette date range (DD/MM/YYYY format). "
            "Filters legislation published on or after this date. "
            "E.g., '01/01/2024' to find legislation from 2024 onwards. "
            "Use with resmi_gazete_tarihi_end for a specific date range."
        ),
    ),
    resmi_gazete_tarihi_end: Optional[str] = Field(
        None,
        description=(
            "End date filter for Official Gazette date range (DD/MM/YYYY format). "
            "Filters legislation published on or before this date. "
            "E.g., '31/12/2024' to find legislation up to end of 2024. "
            "Use with resmi_gazete_tarihi_start for a specific date range."
        ),
    ),
    resmi_gazete_sayisi: Optional[str] = Field(
        None,
        description="Official Gazette issue number filter. E.g., '28513'.",
    ),
    page: int = Field(1, ge=1, description="Page number (1-based, default: 1)"),
    page_size: int = Field(20, ge=1, le=20, description="Results per page (1-20, default: 20)"),
) -> str:
    """
    Search or browse all Turkish legislation on bedesten.adalet.gov.tr.

    Covers 12 legislation types: Kanunlar, Cumhurbaşkanı Kararnameleri, Bakanlar Kurulu Yönetmelikleri,
    CB Yönetmelikleri, CB Kararları, CB Genelgeleri, KHK'lar, Tüzükler,
    Kurum/Kuruluş Yönetmelikleri, Üniversite Yönetmelikleri, Tebliğler, Mülga Mevzuat.

    Search modes:
    - mevzuat_adi: Title/keyword search (recommended, searches legislation name)
    - phrase: Full-text content search (Solr syntax, searches inside document body)
    - mevzuat_no: Direct number lookup (e.g., '5237' for TCK)
    - Browse: Leave all empty to list by type

    Date range filtering:
    - Use resmi_gazete_tarihi_start and/or resmi_gazete_tarihi_end (DD/MM/YYYY)
    - Single date: set both start and end to the same date
    - Year range: '01/01/2024' to '31/12/2024'

    Workflow: Use this tool first to find legislation → then use mevzuatId from results with:
    - get_mevzuat_content: Full document text
    - search_within_mevzuat: Search articles within a document
    - get_mevzuat_madde_tree: Table of contents / article tree
    - get_mevzuat_gerekce: Law rationale (if gerekceId is present in results)
    """
    try:
        tur_list = None
        if mevzuat_tur:
            tur_list = [t.strip().upper() for t in mevzuat_tur.split(",") if t.strip().upper() in _BED_VALID_TYPES]
            if not tur_list:
                return f"Invalid mevzuat_tur: '{mevzuat_tur}'. Valid types: {', '.join(sorted(_BED_VALID_TYPES))}"

        # API requires mevzuatTurList for browsing (no search terms). If no type given, search all.
        if not phrase and not mevzuat_adi and not mevzuat_no and not tur_list:
            tur_list = list(_BED_VALID_TYPES)

        sort_field = "RESMI_GAZETE_TARIHI"
        result = await bedesten_client.search_documents(
            phrase=phrase, mevzuat_adi=mevzuat_adi, mevzuat_no=mevzuat_no,
            mevzuat_tur_list=tur_list, basliktaAra=basliktaAra, tamCumle=tamCumle,
            resmi_gazete_tarihi_start=resmi_gazete_tarihi_start,
            resmi_gazete_tarihi_end=resmi_gazete_tarihi_end,
            resmi_gazete_sayisi=resmi_gazete_sayisi,
            page=page, page_size=page_size,
            sort_field=sort_field, sort_direction="desc",
        )

        if result.error_message:
            return f"Search error: {result.error_message}"

        search_desc = ""
        if phrase:
            search_desc += f"phrase='{phrase}'"
        if mevzuat_adi:
            search_desc += f"{' + ' if search_desc else ''}title='{mevzuat_adi}'"

        if not result.documents:
            return f"No results found for {search_desc or 'browse'}" + (f" (type: {mevzuat_tur})" if mevzuat_tur else "")

        output = []
        if search_desc:
            output.append(f"Search: {search_desc}" + (f" | Type: {mevzuat_tur}" if mevzuat_tur else ""))
        else:
            output.append(f"Browse" + (f" | Type: {mevzuat_tur}" if mevzuat_tur else " | All types"))
        output.append(f"Results: {result.total_results} total (page {page})")
        output.append("")

        for doc in result.documents:
            tur_name = ""
            if isinstance(doc.mevzuat_tur, dict):
                tur_name = doc.mevzuat_tur.get("description", doc.mevzuat_tur.get("name", ""))
            elif isinstance(doc.mevzuat_tur, str):
                tur_name = doc.mevzuat_tur

            line = f"- [{doc.mevzuat_no}] {doc.mevzuat_adi}"
            if tur_name:
                line += f" ({tur_name})"
            line += f" | mevzuatId: {doc.mevzuat_id}"
            if doc.resmi_gazete_tarihi:
                # Format date: strip time portion
                rg = doc.resmi_gazete_tarihi
                if "T" in rg:
                    rg = rg.split("T")[0]
                line += f" | RG: {rg}"
            if doc.gerekce_id:
                line += f" | gerekceId: {doc.gerekce_id}"
            output.append(line)

        return "\n".join(output)

    except Exception as e:
        logger.exception("Error in search_mevzuat")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def get_mevzuat_content(
    mevzuat_id: str = Field(
        ...,
        description=(
            "Legislation ID from search_mevzuat results (mevzuatId field). "
            "This is a string ID (e.g., '345097'), NOT the law number. "
            "First call search_mevzuat to get the mevzuatId."
        ),
    ),
) -> str:
    """
    Retrieve the full content of a Turkish legislation document from bedesten.adalet.gov.tr.

    Returns the complete text in plain format (HTML tags stripped).
    Use mevzuatId from search_mevzuat results (not the law number).

    WARNING: Large legislation (e.g., TCK 5237, TTK 6102) can be 100K+ characters.
    For large documents, prefer search_within_mevzuat to find specific articles
    instead of loading the entire text.

    Workflow: search_mevzuat → get mevzuatId → get_mevzuat_content
    """
    try:
        plain = await bedesten_client.get_document_plain_text(mevzuat_id)
        if not plain:
            return f"Error: No content found for mevzuatId {mevzuat_id}"
        return plain
    except Exception as e:
        logger.exception("Error in get_mevzuat_content")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def search_within_mevzuat(
    mevzuat_id: str = Field(
        ...,
        description=(
            "Legislation ID from search_mevzuat results (mevzuatId field). "
            "This is a string ID (e.g., '345097'), NOT the law number. "
            "First call search_mevzuat to get the mevzuatId."
        ),
    ),
    keyword: str = Field(
        ...,
        description=(
            "Search query with Boolean operators (operators MUST be uppercase). "
            "Simple keyword: 'yatırımcı'. "
            "AND (both required): 'yatırımcı AND tazmin'. "
            "OR (at least one): 'yatırımcı OR müşteri'. "
            "NOT (exclude): 'yatırımcı NOT kurum'. "
            "Exact phrase: '\"mali sıkıntı\"'. "
            "Combined: '\"mali sıkıntı\" AND yatırımcı NOT kurum'."
        ),
    ),
    case_sensitive: bool = Field(False, description="Case-sensitive matching (default: false)"),
    max_results: int = Field(25, ge=1, le=50, description="Maximum number of matching articles to return (1-50, default: 25)"),
) -> str:
    """
    Search within a specific legislation's articles on bedesten.adalet.gov.tr.

    Ideal for large legislation where get_mevzuat_content would return too much text.
    Fetches the full document, splits into individual articles (MADDE), and applies
    keyword search with Boolean operators. Returns only matching articles sorted by
    relevance score (match frequency).

    Each result includes: article number (madde no), match count, and full article text.

    Workflow: search_mevzuat → get mevzuatId → search_within_mevzuat(mevzuatId, keyword)

    Example: To find investor compensation articles in Capital Markets Law:
    1. search_mevzuat(mevzuat_adi='sermaye piyasası', mevzuat_tur='KANUN') → mevzuatId
    2. search_within_mevzuat(mevzuat_id='...', keyword='yatırımcı AND tazmin')
    """
    try:
        plain = await bedesten_client.get_document_plain_text(mevzuat_id)
        if not plain:
            return f"Error: No content found for mevzuatId {mevzuat_id}"

        matches = search_plain_text_articles(plain, keyword, case_sensitive, max_results)

        if not matches:
            return f"No articles matching '{keyword}' found in mevzuatId {mevzuat_id}"

        result = ArticleSearchResult(
            mevzuat_no=str(mevzuat_id),
            mevzuat_tur=0,
            keyword=keyword,
            total_matches=len(matches),
            matching_articles=matches,
        )
        return format_search_results(result)

    except Exception as e:
        logger.exception("Error in search_within_mevzuat")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def get_mevzuat_gerekce(
    gerekce_id: str = Field(
        ...,
        description=(
            "Gerekçe ID from search_mevzuat results (gerekceId field, e.g., '2049'). "
            "Only available for laws (KANUN) that have a published rationale. "
            "Check if gerekceId exists in search_mevzuat results before calling."
        ),
    ),
) -> str:
    """
    Retrieve the law rationale (gerekçe / kanun gerekçesi) from bedesten.adalet.gov.tr.

    The gerekçe contains:
    - Purpose and reasoning behind the law (kanunun amacı ve gerekçesi)
    - Parliamentary committee reports (komisyon raporları)
    - Article-by-article justifications (madde gerekçeleri)

    Only available for KANUN type legislation that has a published rationale.
    Not all laws have a gerekçe — check if gerekceId is present in search_mevzuat results.

    Workflow: search_mevzuat → check gerekceId in results → get_mevzuat_gerekce(gerekceId)
    """
    try:
        result = await bedesten_client.get_gerekce_content(gerekce_id)
        if result.error_message:
            return f"Error fetching gerekçe: {result.error_message}"
        if not result.content:
            return f"Error: No gerekçe content found for gerekceId {gerekce_id}"

        plain = _strip_html(result.content)
        if not plain:
            return f"Error: Gerekçe content is empty for gerekceId {gerekce_id}"

        return plain
    except Exception as e:
        logger.exception("Error in get_mevzuat_gerekce")
        return f"An unexpected error occurred: {str(e)}"


@app.tool()
async def get_mevzuat_madde_tree(
    mevzuat_id: str = Field(
        ...,
        description=(
            "Legislation ID from search_mevzuat results (mevzuatId field). "
            "This is a string ID (e.g., '345097'), NOT the law number. "
            "First call search_mevzuat to get the mevzuatId."
        ),
    ),
) -> str:
    """
    Get the article tree (table of contents / içindekiler) of a Turkish legislation from bedesten.adalet.gov.tr.

    Returns a hierarchical structure showing:
    - Bölüm/Kısım (chapters/parts) as parent nodes
    - Madde (articles) as leaf nodes with maddeId, number, and title
    - Each node may have a gerekceId for article-level rationale

    Works well with: KANUN, CB_KARARNAME, KHK, TUZUK, MULGA.
    May return empty for: CB_KARAR, CB_GENELGE, TEBLIGLER (these often lack structured articles).

    Use this to understand the structure of a large law before diving into specific articles
    with search_within_mevzuat or get_mevzuat_content.

    Workflow: search_mevzuat → get mevzuatId → get_mevzuat_madde_tree(mevzuatId)
    """
    try:
        nodes, err = await bedesten_client.get_article_tree(mevzuat_id)
        if err:
            return f"Article tree not available for mevzuatId {mevzuat_id}: {err}"
        if not nodes:
            return f"No article tree available for mevzuatId {mevzuat_id}."

        output = []
        output.append(f"Article Tree for mevzuatId: {mevzuat_id}")
        flat = _flatten_tree(nodes)
        output.append(f"Total nodes: {len(flat)}")
        output.append("")
        output.append(_format_tree(nodes))

        return "\n".join(output)

    except Exception as e:
        logger.exception("Error in get_mevzuat_madde_tree")
        return f"An unexpected error occurred: {str(e)}"


# ============================================================================
# Ticaret Bakanlığı live catalogue tools
# ============================================================================

_TICARET_CONTENT_KINDS = Literal[
    "mevzuat",
    "destek",
    "veri",
    "rapor",
    "ulke_bilgisi",
    "iletisim",
    "yayin",
]


@app.tool(
    annotations={
        "title": "Resmî tarife tablolarını eşitle",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def sync_official_tariff_data(
    force_refresh: bool = Field(
        False,
        description="True ise Ticaret Bakanlığındaki ZIP arşivleri yeniden kontrol edilir; normalde altı saatlik önbellek kullanılır.",
    ),
) -> TariffSyncStatus:
    """Synchronise versioned 2026 Import Regime and additional-duty tables.

    Archive links are discovered on the current official Ministry landing pages.
    Every workbook is ZIP/CRC checked and SHA-256 versioned. The tool never calls
    the CAPTCHA-protected tariff search engine and never imports third-party rates.
    """
    return await tariff_engine.sync(force=force_refresh)


@app.tool(
    annotations={
        "title": "Resmî AB sınıflandırma karar indeksini eşitle",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def sync_classification_evidence(
    force_refresh: bool = Field(False, description="DG TAXUD sınıflandırma tüzükleri PDF'sini yeniden kontrol eder."),
) -> ClassificationEvidenceStatus:
    """Version the official text-only classification-regulation list.

    EBTI result pages and applicant photographs are not crawled.  The indexed EU
    material is comparative evidence only and is never treated as Turkish GTIP12.
    """
    return await classification_engine.sync(force=force_refresh)


@app.tool(
    app=True,
    annotations={
        "title": "Resmî sınıflandırma gerekçelerinde ara",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def search_classification_evidence(
    query: str = Field(..., min_length=2, max_length=500, description="Ürün tanımı, teknik evsaf veya arama terimleri."),
    code_prefix: Optional[str] = Field(
        None,
        pattern=r"^(?:(?:\d[. ]*){4}|(?:\d[. ]*){6}|(?:\d[. ]*){8}|(?:\d[. ]*){10})$",
        description="Varsa 4/6/8/10 haneli HS/CN kodu.",
    ),
    limit: int = Field(5, ge=1, le=12),
) -> ClassificationEvidenceSearchResult:
    """Retrieve official classification-regulation pages by code and product terms."""
    return await classification_engine.search(query, code_prefix=code_prefix, limit=limit)


@app.tool(
    app=True,
    annotations={
        "title": "Resmî kaynak indeksinde ara (geçmiş tarih dahil)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def search_official_index(
    query: str = Field(..., min_length=2, max_length=300, description="Ürün tanımı, tebliğ adı veya arama terimleri."),
    as_of: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description=(
            "Hangi güne ait sürüm aransın (YYYY-AA-GG). Boş bırakılırsa yalnız bugün yürürlükte "
            "olan sürüm aranır."
        ),
    ),
    gtip_prefix: Optional[str] = Field(None, max_length=20, description="Varsa GTİP ön eki; eşleşen kayıtlar öne alınır."),
    limit: int = Field(10, ge=1, le=30),
) -> dict[str, Any]:
    """Search the versioned official corpora, optionally as they stood on a past date.

    The archive keeps every distinct snapshot, so a past ``as_of`` returns the version that
    was actually in force that day together with its provenance: ``snapshot_id``,
    ``source_sha256`` and the ``as_of_from`` / ``as_of_to`` window it covered.

    A record whose validity window is unknown is **excluded** from a past-dated search:
    presenting an undated row as "in force that day" would be a claim without evidence.
    Rates and control findings still come from the dedicated lookup tools; this tool
    retrieves text, not decisions.
    """
    return await hybrid_index.search(query, limit=limit, gtip_prefix=gtip_prefix, as_of=as_of)


@app.tool(
    app=True,
    annotations={
        "title": "Ürün fotoğrafından görünür evsafları çıkar",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def describe_product_image(
    image_data_url: str = Field(
        ...,
        min_length=28,
        max_length=11_500_000,
        description=(
            "Ürün fotoğrafı: yalnızca data:image/jpeg, data:image/png veya data:image/webp "
            "tipinde base64 data URL. Ham görsel en fazla 8 MB olabilir."
        ),
    ),
) -> dict[str, Any]:
    """Extract editable visible product attributes from a photo; the result is never a GTIP.

    Call this BEFORE suggest_candidate_tariff_codes when the user supplies a photo.
    The output lists only what is visible: material cues, colours, components, label
    text, packaging and the questions still blocking classification. Confirm or repair
    the attributes with the user, then pass the approved textual attributes to
    suggest_candidate_tariff_codes. Never treat the description as a GTIP, tax rate
    or control finding.
    """
    image_bytes, media_type = decode_image_data_url(image_data_url)
    result = await customs_advisor_service.describe_image(image_bytes, media_type)
    return redact_data(result.model_dump(mode="json"), contact_data=True)


@app.tool(
    app=True,
    annotations={
        "title": "Ürün evsafından en yakın üç tarife adayını getir",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def suggest_candidate_tariff_codes(
    product_description: str = Field(..., min_length=12, max_length=2000),
    product_category: str = Field("", max_length=200),
    composition: str = Field("", max_length=500),
    intended_use: str = Field("", max_length=300),
    target_user: str = Field("", max_length=300),
    declared_product_type: str = Field("", max_length=300),
    construction_form: str = Field("", max_length=1000),
    function_mechanism: str = Field("", max_length=1000),
    components_accessories: str = Field("", max_length=1000),
    label_text: str = Field("", max_length=1000),
    visible_features: str = Field("", max_length=2000),
    inferred_features: str = Field("", max_length=1500),
    classification_questions: str = Field("", max_length=1500),
    classification_answers: Optional[list[ClassificationAnswer]] = Field(
        None,
        description="Görsel analizinin sorduğu eksik bilgi sorularına kullanıcının verdiği cevaplar.",
    ),
    origin_country: str = Field("", max_length=100),
) -> ProductClassificationResult:
    """Return up to three non-binding HS6/CN8 candidates from approved product attributes.

    Every candidate is checked for existence in the active official Turkish tariff
    snapshot. When origin is supplied, customs duty and additional-duty rates are
    returned only if every matching 12-digit subline has one unambiguous rate.
    """
    request = ProductClassificationRequest(
        product_description=product_description,
        product_category=product_category,
        composition=composition,
        intended_use=intended_use,
        target_user=target_user,
        declared_product_type=declared_product_type,
        construction_form=construction_form,
        function_mechanism=function_mechanism,
        components_accessories=components_accessories,
        label_text=label_text,
        visible_features=visible_features,
        inferred_features=inferred_features,
        classification_questions=classification_questions,
        classification_answers=classification_answers or [],
        origin_country=origin_country,
    )
    guard_data(request.model_dump(mode="json"), path="MCP ürün evsafı")
    return await customs_advisor_service.classify_product(request)


@app.tool(
    app=True,
    annotations={
        "title": "GTİP ve menşeye göre resmî tarife önlemlerini getir",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def lookup_tariff_measures(
    gtip: str = Field(
        ...,
        pattern=r"^(?:(?:\d[. ]*){6}|(?:\d[. ]*){8}|(?:\d[. ]*){10}|(?:\d[. ]*){12})$",
        description="Noktalı veya düz 6/8 haneli HS/CN ya da 10/12 haneli Türk tarife kodu.",
    ),
    origin_country: Optional[str] = Field(None, max_length=100, description="Menşe ülke; sevk ülkesinden ayrıdır."),
    atr_certificate: Optional[bool] = Field(None, description="Sevk AB'den ise A.TR dolaşım belgesi ibraz edilecek mi? Teyit edilmeden serbest dolaşım sütunu uygulanmaz."),
    dispatch_country: Optional[str] = Field(
        None, max_length=100,
        description="Sevk/çıkış ülkesi menşeden farklıysa; AB'den A.TR ile gelen üçüncü ülke menşeli eşyada gümrük vergisi ve İGV ayrı sütunlardan değerlendirilir.",
    ),
    as_of: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Yürürlük tarihi (YYYY-AA-GG); o gün geçerli olan resmî sürüm seçilir. Boşsa bugün."),
) -> TariffLookupResult:
    """Return source-row-level customs duty and additional-duty evidence.

    A 6/8/10 digit code expands to every matching 12-digit Turkish tariff line.
    Results include rate variants, the selected country column, alternatives,
    conditional end-use rates, workbook/sheet/row, archive checksum and warnings.
    A rate is automatic only when every matching subline has the same unfootnoted rate.
    """
    return await tariff_engine.lookup(
        gtip, origin_country=origin_country, dispatch_country=dispatch_country, atr_certificate=atr_certificate, as_of=as_of
    )


@app.tool(
    app=True,
    annotations={
        "title": "Tescil tarihi için TCMB gümrük kurunu getir",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_customs_exchange_rate(
    currency: str = Field("USD", min_length=3, max_length=5, description="Döviz kodu (USD, EUR, GBP, CNY, JPY...)."),
    registration_date: Optional[str] = Field(
        None,
        description="Beyanname tescil tarihi (YYYY-AA-GG veya GG.AA.YYYY). Boşsa bugün.",
    ),
) -> dict:
    """Return the TCMB selling rate in force on the declaration registration date.

    Gümrük Kanunu md. 30 uyarınca gümrük kıymeti, tescil tarihinde yürürlükte olan
    TCMB döviz satış kuru ile TL'ye çevrilir; yürürlükteki kur, tescil tarihinden
    önceki son iş gününün bültenidir. Sonuç bülten tarihi, numarası ve kaynağı ile
    döner; `calculate_import_landed_cost` çağrısında `exchange_rate` ve
    `exchange_rate_date` olarak kullanılabilir.
    """
    try:
        target = parse_registration_date(registration_date)
        return await exchange_rate_service.customs_quote(currency, target)
    except ExchangeRateError as exc:
        return {"error": str(exc), "currency": currency, "registration_date": registration_date}


@app.tool(
    app=True,
    annotations={
        "title": "Damping, korunma ve gözetim önlemlerini sorgula",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def lookup_trade_measures(
    gtip: str = Field(..., min_length=4, max_length=20, description="4-12 haneli GTİP; kısa kodlar ön ek olarak eşlenir."),
    origin_country: Optional[str] = Field(None, max_length=100, description="Menşe ülke; damping önlemleri ülkeye göre süzülür."),
    as_of: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Yürürlük tarihi (YYYY-AA-GG); süre/bitiş değerlendirmesi o güne göre yapılır."),
) -> dict:
    """Return official anti-dumping / countervailing, safeguard and surveillance coverage for a code.

    Veri, Ticaret Bakanlığı'nın yürürlükteki önlem listeleri ve mevzuat.gov.tr'deki gözetim
    tebliğlerinden günlük eşitlenir. Satırlar GTİP ön ekiyle eşleşir; oran/tutar resmî tabloda
    yazıldığı gibi döner ve hesaba otomatik girilmez.
    """
    try:
        as_of_day = date.fromisoformat(as_of) if as_of else None
        report = trade_measure_engine.lookup(gtip, origin_country, today=as_of_day)
    except ValueError as exc:
        return {"error": str(exc)}
    payload = report.as_dict()
    payload["summary"] = trade_measure_summary(report)
    return payload


@app.tool(
    app=True,
    annotations={
        "title": "Yurt dışı tarifeyi karşılaştır (BK / AB / İsviçre / ABD)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def compare_foreign_tariff(
    gtip: str = Field(..., min_length=6, max_length=20, description="En az 6 haneli GTİP/HS kodu; eşleşme HS-6 düzeyindedir."),
    origin_country: Optional[str] = Field(None, max_length=100, description="Menşe ülke; tercihli oran eşleştirmesi için kullanılır."),
    jurisdiction: str = Field("all", description="uk | eu | ch | us | all"),
    as_of: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Sorgu tarihi (YYYY-AA-GG); resmî bağlantılar bu tarihle açılır."),
) -> dict:
    """Compare the Turkish code against the UK/US tariffs (live APIs) and EU/Swiss official query links.

    Birleşik Krallık ve Amerika Birleşik Devletleri verisi resmî açık API'lerden oran
    düzeyinde çekilir. AB (TARIC/EBTI) ve İsviçre (Tares) makine okunur açık veri
    yayımlamadığı için yalnız resmî sorgu bağlantısı döner. Hiçbir yabancı oran Türkiye
    maliyet hesabına aktarılmaz.
    """
    try:
        result = await foreign_tariff_engine.lookup(
            gtip, origin=origin_country, jurisdiction=jurisdiction, as_of=as_of
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return result.as_dict()


@app.tool(
    app=True,
    annotations={
        "title": "AB Bağlayıcı Tarife Bilgisi kararlarını ara",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def search_eu_bti_decisions(
    gtip: Optional[str] = Field(None, max_length=20, description="4-10 haneli kod; ön ek olarak eşleşir."),
    query: Optional[str] = Field(None, max_length=500, description="Eşya tanımı veya anahtar kelime (kararın kendi dilinde)."),
    limit: int = Field(8, ge=1, le=25, description="Dönecek karar sayısı."),
) -> dict:
    """Search official EU Binding Tariff Information decisions published daily by the Commission.

    Kararlar üye ülke gümrük idarelerince verilir ve resmî EBTI günlük yayınından alınır;
    her satır eşyanın hangi koda, hangi hukuki gerekçeyle sınıflandırıldığını gösterir.
    Bu kararlar Türkiye'de bağlayıcı değildir; karşılaştırmalı kanıt olarak kullanılır.
    """
    try:
        result = ebti_engine.search(query or "", code_prefix=gtip, limit=limit)
    except ValueError as exc:
        return {"error": str(exc)}
    return result.as_dict()


@app.tool(
    app=True,
    annotations={
        "title": "AB TARIC önlemlerini sorgula",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def lookup_eu_taric_measures(
    gtip: str = Field(..., min_length=6, max_length=20, description="En az 6 haneli kod; 10 haneye tamamlanır."),
    origin_country: str = Field("TR", max_length=4, description="Menşe ülke ISO kodu (varsayılan TR)."),
) -> dict:
    """Look up EU TARIC import measures for a goods code and partner country.

    Veri, Komisyonun resmî aylık TARIC ham veri çıkarımından gelir. Özet koşulludur:
    TARIC'te tek bir nihai vergi sayısı yoktur; oran ek koda, kotaya, belgeye ve nihai
    kullanıma bağlıdır. Hiçbir değer Türkiye maliyet hesabına aktarılmaz.
    """
    try:
        result = await eu_taric_engine.lookup(gtip, origin=origin_country)
    except ValueError as exc:
        return {"error": str(exc)}
    return result.as_dict()


@app.tool(
    app=True,
    annotations={
        "title": "AB vergisi, menşe kuralı ve belge şartlarını sorgula (ücretsiz kaynak)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def lookup_eu_access2markets(
    gtip: str = Field(..., min_length=4, max_length=20, description="4-12 haneli kod; ilk 10 hane AB'ye sorulur."),
    origin_country: str = Field("TR", max_length=4, description="Eşyanın menşei (varsayılan TR)."),
    destination_country: str = Field("DE", max_length=4, description="AB üyesi varış ülkesi ISO kodu."),
) -> dict:
    """Look up EU duties, internal taxes and required documents from Access2Markets.

    Komisyonun Access2Markets açık uçlarından gelir ve **ücret doğurmaz**. Varış ülkesi
    AB üyesi değilse oran okunmaz (kaynak o yönde ölçü satırı değil tarife şeması
    döndürüyor). Hiçbir değer Türkiye maliyet hesabına aktarılmaz.
    """
    try:
        result = await access2markets_engine.lookup(
            gtip, origin=origin_country, destination=destination_country
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return result.as_dict()


@app.tool(
    app=True,
    annotations={
        "title": "GTİP'in ÖTV kapsamını sorgula",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def lookup_excise_tax(
    gtip: str = Field(..., min_length=4, max_length=20, description="4-12 haneli GTİP; kısa pozisyon kodları ön ek olarak eşlenir."),
) -> dict:
    """Return whether a code falls under an annexed list of the Turkish excise tax law (ÖTV).

    Kaynak, 4760 sayılı Özel Tüketim Vergisi Kanununun ekli (I)-(IV) sayılı listeleridir.
    Kanun metnindeki oran/tutarlar ile Cumhurbaşkanı kararlarıyla yeniden tespit edilen
    "uygulanacak" değerler ayrı alanlarda döner; ikincisi boşsa kanuni değer geçerlidir.
    (III) sayılı listede sütunlar resmî PDF'te birleşik basıldığından oran gösterilmez.
    Sorgulanan kod listelerde yoksa fakat aynı pozisyonda satır varsa, Armonize Sistem
    revizyonu nedeniyle kodun değişmiş olabileceği uyarısı verilir.
    """
    report = excise_tax_index.lookup(gtip)
    report["summary"] = excise_tax_summary(report)
    return report


@app.tool(
    app=True,
    annotations={
        "title": "İthalat Tebliğleri dizinini listele",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_import_communiques(
    year: Optional[int] = Field(None, ge=2020, le=2100, description="Yıl; boşsa dizindeki tüm yıllar."),
    query: Optional[str] = Field(None, max_length=120, description="Başlıkta aranacak kelime (ör. 'kontenjan', 'gözetim')."),
) -> dict:
    """List the Ministry of Trade import communiqués index (İthalat: YYYY/N) with official links."""
    entries = trade_measure_engine.communiques(year)
    if query:
        needle = query.casefold()
        entries = [entry for entry in entries if needle in entry.get("title", "").casefold()]
    return {
        "count": len(entries),
        "entries": entries[:200],
        "dataset": trade_measure_engine.store.metadata("communiques"),
    }


@app.tool(
    app=True,
    annotations={
        "title": "Beyanname durumunu Eylemio üzerinden sorgula",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def query_customs_declaration_status(
    declaration_no: str = Field(..., min_length=8, max_length=24, description="Beyanname (TCGB) numarası."),
) -> dict:
    """Read declaration detail/status through the Eylemio customs connector (read-only).

    Eylemio'daki 'ticaret-beyanname' konektörü, gümrük müşavirinin BİLGE web servis hesabıyla
    Ticaret Bakanlığı'ndan gerçek beyanname bilgisini okur. Sunucuda EYLEMIO_EMAIL /
    EYLEMIO_PASSWORD tanımlı değilse açıklayıcı hata döner.
    """
    try:
        result = await eylemio_client.declaration_status(declaration_no)
    except EylemioError as exc:
        return {"error": str(exc), "declaration_no": declaration_no}
    result["summary"] = [{"label": label, "value": value} for label, value in summarise_declaration(result)]
    return result


@app.tool(
    app=True,
    annotations={
        "title": "HS/CN adayından Türk GTİP12 karar ağacını aç",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def resolve_turkish_tariff_tree(
    gtip: str = Field(
        ...,
        pattern=r"^(?:(?:\d[. ]*){4}|(?:\d[. ]*){6}|(?:\d[. ]*){8}|(?:\d[. ]*){10}|(?:\d[. ]*){12})$",
        description="Kullanıcının seçtiği 4/6/8/10/12 haneli tarife dalı.",
    ),
    origin_country: Optional[str] = Field(None, max_length=100),
    as_of: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Yürürlük tarihi (YYYY-AA-GG); boşsa bugün."),
) -> TariffDecisionTreeResult:
    """Expose every immediate official child without ranking or auto-selecting one.

    Call repeatedly until GTIP12 when product evidence supports the next branch.
    For a shorter prefix, rates are safe only when returned as unambiguous across
    every descendant.  The tool never proves that the goods belong in a branch.
    """
    return await tariff_engine.decision_tree(gtip, origin_country=origin_country, as_of=as_of)


@app.tool(
    app=True,
    annotations={
        "title": "Kaynaklı ithalat maliyeti hesapla",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def calculate_import_landed_cost(
    gtip: str = Field(
        ...,
        pattern=r"^(?:(?:\d[. ]*){6}|(?:\d[. ]*){8}|(?:\d[. ]*){10}|(?:\d[. ]*){12})$",
        description="6/8/10/12 haneli tarife kodu; kısa kodlar 12 haneli alt satırlara açılır.",
    ),
    origin_country: str = Field(..., min_length=2, max_length=100),
    invoice_value: float = Field(..., gt=0, le=1_000_000_000),
    freight: float = Field(0, ge=0, le=1_000_000_000),
    insurance: float = Field(0, ge=0, le=1_000_000_000),
    other_costs: float = Field(0, ge=0, le=1_000_000_000),
    quantity: Optional[float] = Field(None, gt=0, le=1_000_000_000),
    currency: str = Field("USD", min_length=3, max_length=3),
    vat_rate: Optional[float] = Field(None, ge=0, le=100, description="Resmî kaynaktan doğrulanmış ürüne özel KDV oranı."),
    kkdf_rate: Optional[float] = Field(None, ge=0, le=100, description="Ödeme şekline göre doğrulanmış KKDF oranı; uygulanmıyorsa 0."),
    anti_dumping_amount: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="Ürün/üretici için doğrulanmış damping vergisi toplamı; uygulanmadığı doğrulandıysa 0."),
    sct_amount: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="Doğrulanmış ÖTV toplamı; uygulanmadığı doğrulandıysa 0."),
    surveillance_unit_value: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="Gözetim birim kıymeti; uygulanmadığı doğrulandıysa 0."),
    has_surveillance_certificate: Optional[bool] = Field(None),
    payment_method: Optional[str] = Field(None, max_length=100, description="Ödeme şekli (peşin, mal mukabili, vadeli akreditif, kredili...); KKDF önerisi için."),
    dispatch_country: Optional[str] = Field(None, max_length=100, description="Sevk/çıkış ülkesi menşeden farklıysa (A.TR/serbest dolaşım değerlendirmesi)."),
    atr_certificate: Optional[bool] = Field(None, description="Sevk AB'den ise A.TR dolaşım belgesi ibraz edilecek mi? Teyit edilmeden serbest dolaşım sütunu uygulanmaz."),
    customs_duty_rate: Optional[float] = Field(None, ge=0, le=1000, description="Yalnızca kullanıcıca doğrulanmış gümrük vergisi oranı; resmî orandan farklıysa uyarı döner."),
    additional_duty_rate: Optional[float] = Field(None, ge=0, le=1000, description="Yalnızca kullanıcıca doğrulanmış İGV oranı; resmî orandan farklıysa uyarı döner."),
    additional_financial_liability_rate: Optional[float] = Field(None, ge=0, le=1000, description="Doğrulanmış ek mali yükümlülük oranı; uygulanmıyorsa 0."),
    trt_bandrol_rate: Optional[float] = Field(None, ge=0, le=100, description="TRT bandrol ücreti oranı (uygulanıyorsa)."),
    exchange_rate: Optional[float] = Field(None, gt=0, le=1_000_000, description="Beyanname tescil tarihli TCMB döviz satış kuru (1 birim döviz = kaç TL); verilirse TL özeti üretilir."),
    exchange_rate_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Kur tarihi (YYYY-AA-GG)."),
    stamp_duty_try: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="Beyanname damga vergisi (TL, maktu)."),
    port_storage_try: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="Tescile kadar liman/ardiye/tahmil-tahliye giderleri (TL)."),
    gekap_try: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="GEKAP tutarı (TL, beyanla ödenir)."),
) -> dict:
    """Calculate a reproducible landed cost with official safe-to-use tariff rates.

    Customs duty/İGV are taken only when the current official snapshot resolves one
    unfootnoted rate shared by every matching 12-digit subline for the code and origin.
    VAT, KKDF, dumping, SCT and surveillance facts remain explicit inputs until their
    product-specific official datasets are available. Missing or divergent rates block
    the total instead of becoming zero.
    """
    return await tariff_engine.calculate(
        gtip,
        origin_country,
        LandedCostInput(
            invoice_value=invoice_value,
            freight=freight,
            insurance=insurance,
            other_costs=other_costs,
            quantity=quantity,
            currency=currency,
            anti_dumping_amount=anti_dumping_amount,
            kkdf_rate=kkdf_rate,
            vat_rate=vat_rate,
            sct_amount=sct_amount,
            surveillance_unit_value=surveillance_unit_value,
            has_surveillance_certificate=has_surveillance_certificate,
            payment_method=payment_method,
            customs_duty_rate=customs_duty_rate,
            additional_duty_rate=additional_duty_rate,
            additional_financial_liability_rate=additional_financial_liability_rate,
            trt_bandrol_rate=trt_bandrol_rate,
            exchange_rate=exchange_rate,
            exchange_rate_date=exchange_rate_date,
            stamp_duty_try=stamp_duty_try,
            port_storage_try=port_storage_try,
            gekap_try=gekap_try,
        ),
        dispatch_country=dispatch_country,
        atr_certificate=atr_certificate,
        as_of=as_of,
    )


@app.tool(
    annotations={
        "title": "Resmî Gazete arşivinde mevzuat metni ara",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def search_gazette_archive(
    query: str = Field("", description="Tam metin arama ifadesi (boş bırakılırsa en yeni belgeler)."),
    since: Optional[str] = Field(None, description="Başlangıç tarihi (YYYY-AA-GG)."),
    until: Optional[str] = Field(None, description="Bitiş tarihi (YYYY-AA-GG)."),
    kind: Optional[str] = Field(None, description="Belge ailesi: import_regime, additional_duty, anti_dumping …"),
    limit: int = Field(10, ge=1, le=50),
) -> dict:
    """Search the local Resmî Gazete archive for the customs-relevant legislation text.

    The archive is deliberately selective (import/export regime, additional duties,
    communiques, trade defence, product safety, excise and VAT) and additive: a published
    gazette document never changes, so each document is fetched once and kept with its
    SHA-256, retrieval date and official URL. A document whose extracted text failed the
    readability check is listed with its citation but its body is not searched.
    """
    return resmi_gazete_archive.search(
        query, since=since, until=until, kind=kind, limit=limit
    ).as_dict()


@app.tool(
    annotations={
        "title": "Tarife snapshot değişikliklerini karşılaştır",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def compare_tariff_snapshots(
    source_id: Literal["import_regime", "additional_duty"] = Field(...),
    limit: int = Field(200, ge=1, le=1000),
) -> dict:
    """Compare the two latest official archive snapshots by GTIP, measure and country group."""
    return tariff_engine.changes(source_id, limit=limit)


@app.tool(
    annotations={
        "title": "Güncel ithalat kontrol tebliğlerini eşitle",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def sync_import_control_rules(
    force_refresh: bool = Field(False, description="Resmî konsolide tebliğ metinlerini yeniden kontrol eder."),
) -> ControlSyncStatus:
    """Version and index current Product Safety and Inspection communiques.

    The source is the Ministry of Justice's official Bedesten legislation service.
    GTIP annex scope, TAREKS/risk wording, possible inspection methods and the
    document-list excerpt are stored with a full-text SHA-256 checksum.
    """
    return await control_engine.sync(force=force_refresh)


@app.tool(
    app=True,
    annotations={
        "title": "GTİP için TAREKS, TSE ve ithalat kontrollerini araştır",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def lookup_import_controls(
    gtip: str = Field(..., pattern=r"^(?:\d[. ]*){12}$", description="Noktalı veya düz 12 haneli Türk GTİP."),
    as_of: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Yürürlük tarihi (YYYY-AA-GG); o gün geçerli tebliğ sürümleri kullanılır."),
    direction: str = Field("import", pattern=r"^(import|export)$", description="İşlem yönü: ithalat (varsayılan) veya ihracat."),
) -> ImportControlLookupResult:
    """Find GTIP annex matches without claiming automatic physical inspection.

    A match establishes only that the code appears in an indexed communique
    annex. Product nature, exemptions and the authority's risk result must still
    be checked. Private laboratories are never presented as automatically required.

    ``direction="export"`` searches the export control lists instead. That index is
    **partial** by design — prohibited/licensed export goods, dual-use and sanctions
    lists are not fully indexed — and every export result says so. A miss there never
    means "no obligation".
    """
    return await control_engine.lookup(gtip, as_of=as_of, direction=direction)


@app.tool(
    annotations={
        "title": "İthalat kontrol tebliği sürüm değişikliklerini göster",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def compare_import_control_snapshots(
    rule_code: Optional[str] = Field(None, max_length=20, description="Örn. 2026/18; boşsa tüm tebliğler."),
    limit: int = Field(50, ge=1, le=200),
) -> list[dict]:
    """Return version changes detected in official consolidated control texts."""
    return control_engine.changes(rule_code, limit=limit)


@app.tool(
    annotations={
        "title": "Ticaret Bakanlığı kaynaklarını listele",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def list_ticaret_sources() -> dict:
    """List official Ministry of Trade source trees and live document/page counts.

    Returns separate source families for legislation, state supports, statistics/data,
    reports, country-market information, counsellor/attaché contacts and publications.
    The URLs and counts come from the current live catalogue, not a generated sector list.
    """
    return await ticaret_client.list_sources()


@app.tool(
    app=True,
    annotations={
        "title": "Gümrük ithalat ön değerlendirme kanıtını hazırla",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def prepare_customs_precheck(
    question: str = Field(..., min_length=3, max_length=1500, description="Kullanıcının ithalat/gümrük sorusu."),
    product_description: str = Field("", max_length=2000, description="Teknik ve ticari ürün tanımı."),
    candidate_gtip: Optional[str] = Field(None, max_length=30, description="Varsa resmî karar ağacında kullanıcıca seçilmiş 6/8/10/12 haneli tarife kodu."),
    tariff_selection_confirmed: bool = Field(False, description="Kodun kullanıcı tarafından resmî karar ağacında seçildiği onayı."),
    exact_gtip_confirmed: bool = Field(False, description="Yalnızca doğrulanmış 12 haneli Türk GTİP seçildiyse true."),
    classification_verification_status: Optional[Literal[
        "dual_agreement", "dual_partial_agreement", "arbitrated_disagreement",
        "unresolved_disagreement", "single_model_only",
    ]] = Field(None, description="Bağımsız sınıflandırma modellerinin doğrulama sonucu."),
    classification_confidence_score: Optional[int] = Field(None, ge=0, le=100, description="Resmî kanıt ve model uzlaşmasından türetilen güven puanı."),
    classification_models: Optional[list[str]] = Field(None, max_length=3, description="Sınıflandırmada kullanılan bağımsız model kimlikleri."),
    origin_country: Optional[str] = Field(None, max_length=100, description="Menşe ülke; sevk ülkesinden ayrıdır."),
    dispatch_country: Optional[str] = Field(None, max_length=100, description="Varsa sevk/çıkış ülkesi."),
    atr_certificate: Optional[bool] = Field(None, description="Sevk AB'den ise A.TR dolaşım belgesi ibraz edilecek mi? Teyit edilmeden serbest dolaşım sütunu uygulanmaz."),
    intended_use: Optional[str] = Field(None, max_length=300, description="Ürünün kullanım amacı ve hedef kullanıcısı."),
    target_user: Optional[str] = Field(None, max_length=300, description="Hedef kullanıcının cinsiyet ve yaş grubu."),
    declared_product_type: Optional[str] = Field(None, max_length=300, description="İthalatta beyan edilmesi düşünülen ürün türü."),
    classification_answers: Optional[list[ClassificationAnswer]] = Field(
        None,
        description="Sınıflandırmayı netleştiren kullanıcı soru-cevapları.",
    ),
    composition: Optional[str] = Field(None, max_length=500, description="Malzeme, kimyasal bileşim veya tekstil elyaf oranları."),
    condition: Literal["new", "used", "unknown"] = Field("unknown", description="Ürünün yeni/kullanılmış durumu."),
    invoice_value: Optional[float] = Field(None, gt=0, le=1_000_000_000),
    freight: Optional[float] = Field(None, ge=0, le=1_000_000_000),
    insurance: Optional[float] = Field(None, ge=0, le=1_000_000_000),
    other_pre_import_costs: Optional[float] = Field(None, ge=0, le=1_000_000_000),
    currency: str = Field("USD", min_length=3, max_length=3),
    incoterm: Optional[str] = Field(None, max_length=20),
    payment_method: Optional[str] = Field(None, max_length=80, description="KKDF değerlendirmesi için ödeme şekli."),
    quantity: Optional[float] = Field(None, gt=0, le=1_000_000_000, description="Birim maliyet ve gözetim hesabı için miktar."),
    as_of_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="İşlem tarihi; YYYY-AA-GG."),
    customs_duty_rate: Optional[float] = Field(None, ge=0, le=1000, description="Yalnızca kullanıcıca doğrulanmış oran; araç oran üretmez."),
    additional_duty_rate: Optional[float] = Field(None, ge=0, le=1000, description="Yalnızca kullanıcıca doğrulanmış İGV/ek oran."),
    additional_financial_liability_rate: Optional[float] = Field(None, ge=0, le=1000),
    anti_dumping_amount: Optional[float] = Field(None, ge=0, le=1_000_000_000),
    kkdf_rate: Optional[float] = Field(None, ge=0, le=100),
    vat_rate: Optional[float] = Field(None, ge=0, le=100, description="Yalnızca kullanıcıca doğrulanmış KDV oranı."),
    sct_amount: Optional[float] = Field(None, ge=0, le=1_000_000_000),
    surveillance_unit_value: Optional[float] = Field(None, ge=0, le=1_000_000_000),
    has_surveillance_certificate: Optional[bool] = Field(None),
    trt_bandrol_rate: Optional[float] = Field(None, ge=0, le=100, description="TRT bandrol ücreti oranı (uygulanıyorsa)."),
    exchange_rate: Optional[float] = Field(None, gt=0, le=1_000_000, description="Beyanname tescil tarihli TCMB döviz satış kuru (1 birim döviz = kaç TL); verilirse TL özeti üretilir."),
    exchange_rate_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Kur tarihi (YYYY-AA-GG)."),
    stamp_duty_try: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="Beyanname damga vergisi (TL, maktu)."),
    port_storage_try: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="Tescile kadar liman/ardiye/tahmil-tahliye giderleri (TL)."),
    gekap_try: Optional[float] = Field(None, ge=0, le=1_000_000_000, description="GEKAP tutarı (TL, beyanla ödenir)."),
    direction: Literal["import", "export"] = Field("import", description="İşlem yönü; varsayılan ithalattır."),
    destination_country: Optional[str] = Field(None, max_length=100, description="İhracatta eşyanın gideceği ülke; ihracat modunda zorunludur."),
) -> CustomsEvidencePack:
    """Prepare the evidence required for a cautious product-specific import answer.

    Returns current official Turkish sources for GTIP/BTB, TAREKS, TSE, product-safety
    communiques, chemicals, customs value, tax and trade-defence checks plus comparative
    EU EBTI/CLASS/CN/TARIC sources. EU codes are not Turkish GTIP12 or Turkish tax rates.
    The caller must cite the returned source IDs, ask for the listed missing fields, and
    include legal_notice verbatim. A product image may suggest questions/candidates but
    cannot create a binding classification. Exact rates must not be inferred when the
    official evidence does not directly establish product, origin and date applicability.
    """
    inquiry = CustomsInquiry(
        question=question,
        product_description=product_description,
        candidate_gtip=candidate_gtip,
        tariff_selection_confirmed=tariff_selection_confirmed,
        exact_gtip_confirmed=exact_gtip_confirmed,
        classification_verification_status=classification_verification_status,
        classification_confidence_score=classification_confidence_score,
        classification_models=classification_models or [],
        origin_country=origin_country,
        dispatch_country=dispatch_country,
        atr_certificate=atr_certificate,
        intended_use=intended_use,
        target_user=target_user,
        declared_product_type=declared_product_type,
        classification_answers=classification_answers or [],
        composition=composition,
        condition=condition,
        invoice_value=invoice_value,
        freight=freight,
        insurance=insurance,
        other_pre_import_costs=other_pre_import_costs,
        currency=currency,
        incoterm=incoterm,
        payment_method=payment_method,
        quantity=quantity,
        as_of_date=as_of_date,
        customs_duty_rate=customs_duty_rate,
        additional_duty_rate=additional_duty_rate,
        additional_financial_liability_rate=additional_financial_liability_rate,
        anti_dumping_amount=anti_dumping_amount,
        kkdf_rate=kkdf_rate,
        vat_rate=vat_rate,
        sct_amount=sct_amount,
        surveillance_unit_value=surveillance_unit_value,
        has_surveillance_certificate=has_surveillance_certificate,
        trt_bandrol_rate=trt_bandrol_rate,
        exchange_rate=exchange_rate,
        exchange_rate_date=exchange_rate_date,
        stamp_duty_try=stamp_duty_try,
        port_storage_try=port_storage_try,
        gekap_try=gekap_try,
        direction=direction,
        destination_country=destination_country,
    )
    guard_data(inquiry.model_dump(mode="json"), path="MCP gümrük sorusu")
    return await customs_advisor_service.evidence_pack(inquiry)


@app.tool(
    annotations={
        "title": "Ticaret Bakanlığı kataloğunda ara",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def search_ticaret_catalog(
    query: str = Field(
        "",
        max_length=300,
        description=(
            "Turkish keywords, document number, country, sector, support name or contact term. "
            "Examples: 'Gümrük Kanunu 4458', '5973 pazara giriş desteği', "
            "'Çin kozmetik sektör raporu', 'Kabil Ticaret Müşavirliği'. Leave empty to browse with filters."
        ),
    ),
    content_kinds: Optional[list[_TICARET_CONTENT_KINDS]] = Field(
        None,
        description=(
            "Optional separate information layers: mevzuat, destek, veri, rapor, "
            "ulke_bilgisi, iletisim, yayin. Leave empty to search all layers."
        ),
    ),
    source_ids: Optional[list[str]] = Field(
        None,
        description="Optional source IDs returned by list_ticaret_sources, e.g. ['gumruk', 'destekler'].",
    ),
    document_types: Optional[list[str]] = Field(
        None,
        description="Optional types such as Kanun, Yönetmelik, Tebliğ, Genelge, Karar, Rapor, İstatistik or Rehber.",
    ),
    year: Optional[int] = Field(None, ge=1900, le=2100, description="Optional year filter."),
    include_repealed: bool = Field(
        False,
        description="Include records visibly marked Mülga/yürürlükten kaldırılmış. Default false for safer current-law analysis.",
    ),
    offset: int = Field(0, ge=0, le=100000, description="Zero-based result offset."),
    limit: int = Field(20, ge=1, le=50, description="Maximum results to return (1-50)."),
) -> TicaretSearchResult:
    """Search the continuously refreshed Ministry of Trade information catalogue.

    Covers customs, import/export rules and implementation notices, state support
    decisions/circulars, internal trade, consumer and product-safety rules, free zones,
    service trade, official statistics/data links, Ministry publications, country-market
    and commercial counsellor/attaché reports, and overseas contact information.

    This is metadata and live-page-summary search. Use get_ticaret_document for a selected
    result's full text or search_ticaret_content to scan selected candidates. Every result
    carries both the document URL and the Ministry source-page URL.
    """
    return await ticaret_client.search(
        query=query,
        content_kinds=list(content_kinds) if content_kinds else None,
        source_ids=source_ids,
        document_types=document_types,
        year=year,
        include_repealed=include_repealed,
        offset=offset,
        limit=limit,
    )


@app.tool(
    annotations={
        "title": "Ticaret Bakanlığı belgesini getir",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_ticaret_document(
    document_id: str = Field(
        ...,
        pattern=r"^ticaret_[0-9a-f]{24}$",
        description="Document ID returned by search_ticaret_catalog. URLs are not accepted; this prevents arbitrary downloads.",
    ),
    offset: int = Field(0, ge=0, le=10000000, description="Character offset for reading a later part of a long document."),
    max_characters: int = Field(
        40000,
        ge=1000,
        le=100000,
        description="Maximum extracted characters returned in this call. Continue with offset when truncated=true.",
    ),
) -> TicaretDocumentContent:
    """Fetch and extract a bounded slice of an official Ministry document or page.

    Supports live HTML and official PDF, DOCX, XLSX, CSV, text and ZIP resources.
    ZIP contents are listed and supported members are extracted within safety limits.
    Large documents are paginated by character offset. The response includes resolved
    official URL, source metadata, total size, truncation state and extraction warnings.
    """
    return await ticaret_client.get_document_content(
        document_id,
        offset=offset,
        max_characters=max_characters,
    )


@app.tool(
    annotations={
        "title": "Ticaret Bakanlığı belgelerinde tam metin ara",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def search_ticaret_content(
    query: str = Field(
        ...,
        min_length=2,
        max_length=300,
        description="Text or phrase to find inside official pages/documents.",
    ),
    document_ids: Optional[list[str]] = Field(
        None,
        max_length=25,
        description=(
            "Preferred precise mode: IDs returned by search_ticaret_catalog. "
            "Provide up to 25 IDs to scan those exact documents."
        ),
    ),
    content_kinds: Optional[list[_TICARET_CONTENT_KINDS]] = Field(
        None,
        description="Optional information-layer filters when document_ids is omitted.",
    ),
    source_ids: Optional[list[str]] = Field(
        None,
        description="Optional source IDs when document_ids is omitted.",
    ),
    year: Optional[int] = Field(None, ge=1900, le=2100, description="Optional year filter."),
    include_repealed: bool = Field(False, description="Include visibly repealed records in candidate selection."),
    max_documents_to_scan: int = Field(
        12,
        ge=1,
        le=25,
        description="Maximum documents downloaded and scanned in this call (1-25).",
    ),
    max_results: int = Field(10, ge=1, le=20, description="Maximum matching documents returned (1-20)."),
) -> TicaretContentSearchResult:
    """Search inside a bounded set of live Ministry pages and downloadable documents.

    For broad research, first narrow candidates with search_ticaret_catalog and then pass
    their IDs here in batches. The response explicitly reports candidate and scanned counts,
    so a partial scan cannot be mistaken for an exhaustive legal conclusion.
    """
    return await ticaret_client.search_content(
        query=query,
        document_ids=document_ids,
        content_kinds=list(content_kinds) if content_kinds else None,
        source_ids=source_ids,
        year=year,
        include_repealed=include_repealed,
        max_documents_to_scan=max_documents_to_scan,
        max_results=max_results,
    )


@app.tool(
    annotations={
        "title": "Ticaret Bakanlığı katalog güncelliğini göster",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_ticaret_catalog_status(
    force_refresh: bool = Field(
        False,
        description=(
            "When true, wait for a full live rescan before returning. This can take several minutes; "
            "normally leave false because priority legislation is refreshed hourly and the complete "
            "catalogue is refreshed every six hours."
        ),
    ),
) -> TicaretCatalogStatus:
    """Return catalogue freshness, next sync time, coverage counts and source errors."""
    if force_refresh:
        await ticaret_client.refresh_catalog()
    return ticaret_client.status()


def main():
    logger.info(f"Starting {app.name} server...")
    try:
        app.run()
    except KeyboardInterrupt:
        logger.info(f"{app.name} server shut down by user.")
    except Exception:
        logger.exception(f"{app.name} server crashed.")


if __name__ == "__main__":
    main()
