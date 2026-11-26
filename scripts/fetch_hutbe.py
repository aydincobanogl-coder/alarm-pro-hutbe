#!/usr/bin/env python3
"""
Diyanet İşleri Başkanlığı Din Hizmetleri Genel Müdürlüğü sayfasından
haftalık Cuma (ve Bayram) hutbelerini otomatik çeker, temiz JSON formatına dönüştürür
ve Türkçe kısımları Microsoft Neural Türkçe ses (tr-TR-AhmetNeural),
Arapça ayet ve hadisleri ise fasih Arapça Neural ses (ar-SA-HamedNeural) ile
doğal biçimde birleştirerek sesli hutbe MP3 üretir.
"""

import asyncio
import datetime
import html
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from pypdf import PdfReader
import edge_tts

BASE_URL = "https://dinhizmetleri.diyanet.gov.tr"
RAW_BASE_URL = "https://raw.githubusercontent.com/aydincobanogl-coder/alarm-pro-hutbe/main"
REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = REPO_ROOT / "audio"
OUTPUT_JSON = REPO_ROOT / "hutbe.json"
MAX_ARCHIVE = 6

# Ses sürümü yükseltildiğinde mevcut tüm hutbe MP3'leri otomatik yeniden üretilir
# ve kullanıcı telefonlarındaki eski önbellek atlanır.
AUDIO_VERSION = 2
TURKISH_VOICE = "tr-TR-AhmetNeural"
ARABIC_VOICE = "ar-SA-HamedNeural"

# ISO/IEC 13818-3 uyumlu MPEG-2 Layer III (24kHz, 48kbps, mono) 24ms saf sessizlik karesi
# (edge-tts'in hem tr-TR-AhmetNeural hem ar-SA-HamedNeural çıktısıyla birebir aynı format)
SILENT_MP3_FRAME_24MS = b"\xff\xf3\x64\xc4" + (b"\x00" * 140)

AYLAR_TR = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"
]
GUNLER_TR = [
    "Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}


def encode_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    raw_path = urllib.parse.unquote(parsed.path)
    encoded_path = urllib.parse.quote(raw_path, safe="/:()-_.,!~'")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, encoded_path, parsed.query, parsed.fragment))


def fetch_bytes(url: str, retries: int = 3, timeout: int = 30) -> bytes:
    safe_url = encode_url(url)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                [
                    "curl", "-sS", "-L", "--fail",
                    "--connect-timeout", "12",
                    "--max-time", str(timeout),
                    "-A", HEADERS["User-Agent"],
                    "-H", f"Accept-Language: {HEADERS['Accept-Language']}",
                    safe_url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout + 5,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
            raise RuntimeError(f"curl exit {proc.returncode}: {proc.stderr.decode('utf-8', 'ignore').strip()}")
        except Exception as e:
            last_err = e
            print(f"[WARN] Attempt {attempt}/{retries} failed for {safe_url}: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {safe_url}: {last_err}")


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="ignore")


def has_arabic(text: str) -> bool:
    return any(
        ('\u0600' <= ch <= '\u06FF')
        or ('\u0750' <= ch <= '\u077F')
        or ('\u08A0' <= ch <= '\u08FF')
        or ('\uFB50' <= ch <= '\uFDFF')
        or ('\uFE70' <= ch <= '\uFEFF')
        for ch in text
    )


def format_turkish_date(ddmmyyyy: str) -> tuple[str, str]:
    """'27.11.2026' -> ('27 Kasım 2026 Cuma', '2026-11-27')"""
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", ddmmyyyy.strip())
    if not m:
        return ddmmyyyy, ""
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        dt = datetime.date(year, month, day)
        gun_adi = GUNLER_TR[dt.weekday()]
        return f"{day} {AYLAR_TR[month]} {year} {gun_adi}", dt.isoformat()
    except ValueError:
        return ddmmyyyy, ""


def tr_title_case(s: str) -> str:
    """Türkçe harflere duyarlı başlık düzenleme (yalnızca tamamen büyük harfse uygular)."""
    s = s.strip()
    if not s or not s.isupper():
        return s
    lower_map = str.maketrans({"I": "ı", "İ": "i"})
    upper_map = str.maketrans({"ı": "I", "i": "İ"})
    words = s.translate(lower_map).lower().split()
    minor = {"ve", "ile", "de", "da", "mi", "mı", "mu", "mü", "için"}
    out = []
    for idx, w in enumerate(words):
        if idx > 0 and w in minor:
            out.append(w)
        else:
            out.append(w[0].translate(upper_map).upper() + w[1:])
    return " ".join(out)


def clean_inline_footnotes_for_display(text: str) -> str:
    """PDF'den gelen 'olur.2' veya '...işidir.”1' gibi bitişik dipnot numaralarını zarif hale getirir."""
    text = re.sub(r"([.!?\"”’…])\s*(\d{1,2})(?=\s|$|,|;)", r"\1 [\2]", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize_arabic_display(text: str) -> str:
    """
    Diyanet PDF'lerinden pypdf ile çıkarılan Arapça satırlardaki font/ligatür bozulmalarını
    (eksik ه harfi, şedde yerine boşluk/üst elif, kelimeden kopan ة vb.) hem temiz görünüm
    hem de doğru Arapça kıraat için onarır.
    """
    if not text:
        return ""
    lines_out: list[str] = []
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue

        # 1. Diyanet PDF'lerinde şedde karakteri bazen kelime içinde boşluk + hareke olarak çıkar
        # (örn: "فَلَنُحْيِيَن َهُ", "طَي ِبَةً", "إِن َ", "قَل َ", "صَل َي", "وَسَل َمَ")
        # Önce lafza-i celal kalıplarını onar
        ln = re.sub(r"الل[\u0651\u0670\s]+([َُِ])(?=\s|$|[.:،؛؟…])", r"اللّٰه\1", ln)
        ln = re.sub(r"الل[\u0651\u0670]{2,}(?=\s|$|[.:،؛؟…])", "اللّٰهُ", ln)

        # Şedde yerine boşluk + hareke gelen durumları düzelt (kelime sonu noktalama hariç)
        ln = re.sub(r"(?<=[\u0621-\u064A])\s+([\u064E\u064F\u0650])(?=[\u0621-\u064A])", "\u0651\\1", ln)
        ln = re.sub(r"\bإِن\s+َ\b", "إِنَّ", ln)
        ln = re.sub(r"\bقَل\s+َ(?=\s|$|[.:،؛؟…])", "قَلَّ", ln)
        ln = re.sub(r"\bأَحَ\s+ب\s*َ\b", "أَحَبَّ", ln)

        # 2. Harekelerin önündeki fazladan boşlukları temizle
        ln = re.sub(r"\s+([\u064B-\u065F\u0670\u06D6-\u06ED])", r"\1", ln)

        # 3. Kelimeden kopan ة (ta-i merbuta) ekini birleştir (örn: "الْبَتَٰ ةَ")
        ln = re.sub(r"([\u0621-\u064A\u064B-\u065F\u0670])\s+(ة[\u064B-\u065F]*)", r"\1\2", ln)

        # 4. Bazı Diyanet PDF fontlarında tüm üstünler (fatha) şedde+üstün olarak çıkar (>%28 şedde yoğunluğu)
        ar_letters = len(re.findall(r"[\u0621-\u064A]", ln))
        shaddas = ln.count("\u0651")
        if ar_letters >= 10 and (shaddas / ar_letters) > 0.28:
            ln = ln.replace("\u0651", "")
            ln = re.sub(r"الل[َُِٰ]*ه", "اللّٰه", ln)
            ln = ln.replace("الرَحْم", "الرَّحْم").replace("الرَحِيم", "الرَّحِيم")
            ln = ln.replace("الَذ", "الَّذ").replace("النَاس", "النَّاس")
            ln = ln.replace("ثُمَ", "ثُمَّ").replace("إِنَ", "إِنَّ").replace("اِنَ", "إِنَّ")
            ln = ln.replace("كَاَنَ", "كَأَنَّ").replace("إِيَاكُمْ", "إِيَّاكُمْ")

        # 5. Diyanet PDF'lerinde hareke önündeki ٰ (U+0670) aslında şeddedir (örn: الْجَنَٰةَ -> الْجَنَّةَ)
        ln = re.sub(r"\u0670([\u064E\u064F\u0650])", "\u0651\\1", ln)
        ln = re.sub(r"\u0670\u0651", "\u0651", ln)

        # 6. Salavat kalıbı ("وَقَالَ رَسُولُ اللّٰهِ صَلَّى اللّٰهُ عَلَيْهِ وَسَلَّمَ")
        ln = re.sub(r"\bوَ\s+قَالَ\b", "وَقَالَ", ln)
        ln = re.sub(r"صَ?لّ?َ?[يى]\s+اللّٰهُ", "صَلَّى اللّٰهُ", ln)
        ln = re.sub(r"صَلَّي(?=\s|$)", "صَلَّى", ln)
        ln = re.sub(r"وَسَلّ?َ?مَ?(?=\s|$|[.:،؛؟…])", "وَسَلَّمَ", ln)

        # 7. Yaygın PDF hemze / elif hataları
        ln = ln.replace("وَيَسْـَلُونَكَ", "وَيَسْأَلُونَكَ")
        ln = ln.replace("اِصْلََحٌ", "إِصْلَاحٌ")
        ln = ln.replace("لَِِهْلِهِ", "لِأَهْلِهِ")
        ln = re.sub(r"اُ\s*و۬لٰ+ٓئِكَ?", "أُولَٰئِكَ", ln)
        ln = re.sub(r"([\u064B-\u0652])\1+", r"\1", ln)
        ln = re.sub(r"\s{2,}", " ", ln).strip()
        lines_out.append(ln)
    return "\n".join(lines_out)


def clean_arabic_for_tts(text: str) -> list[tuple[str, int]]:
    """
    Arapça metni fasih Arapça kıraate (ar-SA-HamedNeural) uygun cümle parçalarına
    ve duraklama sürelerine (ms) dönüştürür.
    """
    norm = normalize_arabic_display(text)
    if not norm:
        return []

    s = norm
    # Osmanlı/Resm-i Osmani imlasını modern fasih Arapça TTS imlasına çevir
    s = s.replace("اللّٰه", "اللَّه")
    s = s.replace("الرَّحْمٰن", "الرَّحْمَن").replace("الرَّحْمٰن", "الرَّحْمَن")
    s = s.replace("الْيَتَامٰى", "الْيَتَامَى")
    s = s.replace("حَيٰوةً", "حَيَاةً").replace("حَيٰوةٌ", "حَيَاةٌ").replace("حَيٰوةٍ", "حَيَاةٍ")
    s = s.replace("صَلٰوة", "صَلَاة").replace("زَكٰوة", "زَكَاة")
    s = s.replace("أُولَٰئِكَ", "أُولَئِكَ")
    s = s.replace("اٰ", "آ")
    s = s.replace("ٰى", "َى")
    # Hareke taşıyan çıplak elifler fasih Arapça'da hemze-i kat'dır (اَ -> أَ, اِ -> إِ, اُ -> أُ)
    s = s.replace("اَ", "أَ").replace("اِ", "إِ").replace("اُ", "أُ")
    s = s.replace("ـ", "")
    # TTS'nin okuyamadığı durak işaretleri ve Osmanlıca imale/med işaretlerini temizle
    s = re.sub(r"[\u06EA\u06EC\u06D6-\u06ED\u06DF-\u06E8]", "", s)
    s = s.replace("\u0670", "ا")
    # Üç nokta ve süsleme işaretlerini temizle
    s = re.sub(r"\.{2,}|…+", " ", s)
    s = re.sub(r"\s*[:،؛]\s*", " ", s)

    raw_lines = [re.sub(r"\s{2,}", " ", ln).strip(" .،؛:") for ln in s.splitlines()]
    raw_lines = [ln for ln in raw_lines if ln and has_arabic(ln)]
    if not raw_lines:
        return []

    # Mantıksal kıraat bölümleri: Besmele / Ayet / "Ve kale Resulullahi..." / Hadis
    besmele_lines: list[str] = []
    ayet_lines: list[str] = []
    salavat_line = ""
    hadis_lines: list[str] = []

    stage = "start"
    for ln in raw_lines:
        if stage == "start" and ("بِسْمِ اللَّهِ" in ln or "بسم الله" in ln):
            besmele_lines.append(ln)
            stage = "ayet"
        elif re.search(r"قَالَ\s+رَسُولُ", ln):
            salavat_line = ln
            stage = "hadis"
        elif stage == "hadis":
            hadis_lines.append(ln)
        else:
            ayet_lines.append(ln)
            stage = "ayet"

    clauses: list[tuple[str, int]] = []
    if besmele_lines:
        clauses.append((" ".join(besmele_lines) + ".", 480))
    if ayet_lines:
        clauses.append((" ".join(ayet_lines) + ".", 560))
    if salavat_line:
        clauses.append((salavat_line + ":", 320))
    if hadis_lines:
        clauses.append((" ".join(hadis_lines) + ".", 650))

    return clauses


def clean_turkish_text_for_tts(text: str) -> str:
    """Türkçe hutbe paragrafını akıcı ve doğal okunuş için temizler."""
    t = text
    # Dipnot referanslarını [1], [2] veya bitişik rakamları temizle
    t = re.sub(r"\[\d{1,2}\]", "", t)
    t = re.sub(r"([.!?\"”’…])\s*\d{1,2}(?=\s|$|,|;)", r"\1", t)
    # Dini kısaltmaları saygılı ve doğal okunuşuna aç
    t = re.sub(r"\(\s*s\.?\s*a\.?\s*[sv]\.?\s*\)", " sallallahu aleyhi ve sellem ", t, flags=re.I)
    t = re.sub(r"\(\s*a\.?\s*s\.?\s*\)", " aleyhisselam ", t, flags=re.I)
    t = re.sub(r"\(\s*r\.?\s*a\.?\s*\)", " radıyallahu anh ", t, flags=re.I)
    t = re.sub(r"\(\s*c\.?\s*c\.?\s*\)", " celle celaluhu ", t, flags=re.I)
    t = re.sub(r"\bHz\.\s*", "Hazreti ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def build_tts_segments(
    baslik: str,
    tarih_uzun: str,
    arapca_giris: str,
    bloklar: list[dict],
) -> list[tuple[str, str, int]]:
    """
    Hutbenin tamamını sırasıyla (dil, metin, sonrasındaki_duraklama_ms) parçalarına ayırır:
    - Türkçe giriş başlığı (tr-TR-AhmetNeural)
    - Arapça açılış ayet ve hadisleri (ar-SA-HamedNeural)
    - Hutbe gövdesi: Türkçe paragraflar (tr-TR-AhmetNeural) ve varsa gövde içi Arapça bloklar (ar-SA-HamedNeural)
    """
    segments: list[tuple[str, str, int]] = []

    intro = f"{tarih_uzun} Cuma Hutbesi. Konumuz: {baslik}."
    segments.append(("tr", intro, 480))

    # Arapça açılış ayet ve hadisleri -> Fasih Arapça Yapay Zeka Sesi
    if arapca_giris:
        for ar_clause, pause_ms in clean_arabic_for_tts(arapca_giris):
            segments.append(("ar", ar_clause, pause_ms))

    # Gövde bloklarını dil değişim noktalarından bölerek sırayla ekle
    tr_buffer: list[str] = []

    def flush_tr(pause_ms: int = 420):
        if tr_buffer:
            joined = "\n\n".join(tr_buffer).strip()
            if joined:
                segments.append(("tr", joined, pause_ms))
            tr_buffer.clear()

    for b in bloklar:
        tip = b.get("tip", "paragraf")
        metin = b.get("metin", "")
        if tip == "arapca":
            flush_tr(pause_ms=420)
            for ar_clause, pause_ms in clean_arabic_for_tts(metin):
                segments.append(("ar", ar_clause, pause_ms))
        elif tip in ("hitap", "paragraf", "alinti"):
            cleaned = clean_turkish_text_for_tts(metin)
            if cleaned:
                tr_buffer.append(cleaned)

    flush_tr(pause_ms=0)
    return segments


def is_salutation(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 55:
        return False
    if s.endswith("!") and len(s.split()) <= 6:
        return True
    salutation_keywords = (
        "Muhterem", "Aziz", "Değerli", "Kıymetli", "Sevgili", "Kardeşlerim",
        "Müslümanlar", "Müminler", "Gençler", "Anneler", "Babalar"
    )
    return s.endswith("!") and any(k in s for k in salutation_keywords)


def parse_hutbe_pdf(pdf_bytes: bytes, fallback_title: str, fallback_date: str) -> dict:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    raw_text = "\n".join((page.extract_text() or "") for page in reader.pages)
    lines = [ln.strip() for ln in raw_text.splitlines()]

    tarih_str = fallback_date
    arapca_lines: list[str] = []
    title_from_pdf = ""
    body_lines: list[str] = []
    dipnotlar: list[str] = []

    in_footnotes = False
    started_body = False

    for raw_ln in lines:
        ln = re.sub(r"\s+", " ", raw_ln).strip()
        if not ln:
            continue
        if ln.lower().startswith("tarih:"):
            m_date = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", ln)
            if m_date:
                tarih_str = m_date.group(1)
            continue
        if "din hizmetleri genel müdürlüğü" in ln.lower():
            continue

        # Dipnot başlangıcı: "1 Bakara, 2/220." gibi
        if started_body and re.match(r"^\d{1,2}\s+[A-ZÇĞİÖŞÜ]", ln):
            in_footnotes = True
            m_fn = re.match(r"^(\d{1,2})\s+(.*)$", ln)
            if m_fn:
                dipnotlar.append(f"{m_fn.group(1)}. {m_fn.group(2).strip()}")
            continue

        if in_footnotes:
            if dipnotlar:
                dipnotlar[-1] = dipnotlar[-1] + " " + ln
            continue

        # Başlangıçtaki Arapça ayet/hadis satırları
        if not started_body and has_arabic(ln):
            if ln == "﷽":
                arapca_lines.append("بِسْمِ اللّٰهِ الرَّحْمٰنِ الرَّحِيمِ")
            else:
                arapca_lines.append(ln)
            continue

        # İlk Türkçe hitap satırından önceki satır PDF başlığıdır
        if not started_body:
            if is_salutation(ln):
                started_body = True
                body_lines.append(ln)
            else:
                title_from_pdf = (title_from_pdf + " " + ln).strip()
            continue

        body_lines.append(ln)

    # Gövde satırlarını akıllı paragraf bloklarına dönüştür
    bloklar: list[dict] = []
    current_para: list[str] = []

    def flush_para():
        if not current_para:
            return
        joined = ""
        for seg in current_para:
            if joined.endswith("-") and not joined.endswith(" -"):
                joined = joined[:-1] + seg
            elif joined:
                joined = joined + " " + seg
            else:
                joined = seg
        joined = clean_inline_footnotes_for_display(joined)
        if joined:
            if (joined.startswith("“") or joined.startswith('"')) and len(joined) < 320:
                bloklar.append({"tip": "alinti", "metin": joined})
            else:
                bloklar.append({"tip": "paragraf", "metin": joined})
        current_para.clear()

    for ln in body_lines:
        if is_salutation(ln):
            flush_para()
            bloklar.append({"tip": "hitap", "metin": ln})
        elif has_arabic(ln) and not re.search(r"[a-zçğıöşü]", ln):
            flush_para()
            bloklar.append({"tip": "arapca", "metin": normalize_arabic_display(ln)})
        else:
            current_para.append(ln)
    flush_para()

    final_title = fallback_title.strip() if fallback_title.strip() else tr_title_case(title_from_pdf)
    if not final_title:
        final_title = "Cuma Hutbesi"

    tarih_uzun, tarih_iso = format_turkish_date(tarih_str)
    word_count = sum(len(b["metin"].split()) for b in bloklar)
    okuma_dk = max(2, round(word_count / 145))

    return {
        "tarih": tarih_str,
        "tarih_uzun": tarih_uzun,
        "tarih_iso": tarih_iso,
        "baslik": final_title,
        "okuma_suresi": f"{okuma_dk} dk okuma",
        "arapca_giris": normalize_arabic_display("\n".join(arapca_lines).strip()),
        "bloklar": bloklar,
        "dipnotlar": dipnotlar,
    }


def discover_hutbe_links(limit: int = MAX_ARCHIVE) -> list[dict]:
    """Diyanet Din Hizmetleri ana sayfasından en güncel hutbe detay sayfalarını bulur."""
    home_html = html.unescape(fetch_text(BASE_URL + "/"))
    pattern = re.compile(
        r'href=["\']((?:https?://dinhizmetleri\.diyanet\.gov\.tr)?/Detay/(\d+)/([^"\']*hutbe[^"\']*))["\']',
        re.I,
    )
    seen_ids = set()
    items = []
    for match in pattern.finditer(home_html):
        href, detay_id, slug = match.group(1), match.group(2), match.group(3)
        if detay_id in seen_ids:
            continue
        seen_ids.add(detay_id)
        full_url = href if href.startswith("http") else (BASE_URL + href)
        date_str = ""
        m_dt = re.match(r"(\d{2})(\d{2})(\d{4})-", slug)
        if m_dt:
            date_str = f"{m_dt.group(1)}.{m_dt.group(2)}.{m_dt.group(3)}"
        items.append({
            "id": detay_id,
            "url": full_url,
            "date_hint": date_str,
            "slug": slug,
        })
        if len(items) >= limit:
            break
    return items


def extract_attachments(detail_html: str) -> tuple[str, str, str]:
    """
    Detay sayfasından (turkce_pdf_url, turkce_baslik, diyanet_mp3_url) döner.
    """
    unescaped = html.unescape(detail_html)
    anchors = re.findall(
        r'<a[^>]+href=["\'](/Documents/[^"\']+)["\'][^>]*>(.*?)</a>',
        unescaped,
        re.I | re.S,
    )
    pdf_url = ""
    pdf_title = ""
    mp3_url = ""

    for href, inner in anchors:
        label = re.sub(r"<[^>]+>", "", inner).strip()
        lower_href = href.lower()
        if lower_href.endswith(".mp3") and not mp3_url:
            mp3_url = encode_url(BASE_URL + href)
        elif lower_href.endswith(".pdf") and not pdf_url:
            if not has_arabic(label) and not has_arabic(href):
                pdf_url = encode_url(BASE_URL + href)
                pdf_title = label

    return pdf_url, pdf_title, mp3_url


async def _synthesize_segment(lang: str, text: str, retries: int = 3) -> bytes:
    """Tek bir Türkçe veya Arapça bölümü uygun Neural AI sesiyle ham MP3 byte'larına çevirir."""
    if lang == "ar":
        voice = ARABIC_VOICE
        rate = "-14%"
        pitch = "-2Hz"
    else:
        voice = TURKISH_VOICE
        rate = "-5%"
        pitch = "-2Hz"

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            chunks = bytearray()
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate,
                pitch=pitch,
            )
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.extend(chunk["data"])
            if len(chunks) > 500:
                return bytes(chunks)
            raise RuntimeError("Boş ses akışı döndü")
        except Exception as e:
            last_err = e
            print(f"[WARN] TTS ({lang}/{voice}) deneme {attempt}/{retries} başarısız: {e}", file=sys.stderr)
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"TTS segment failed ({lang}): {last_err}")


async def generate_ai_audio(segments: list[tuple[str, str, int]], out_path: Path) -> bool:
    """
    Türkçe (tr-TR-AhmetNeural) ve Arapça (ar-SA-HamedNeural) parçaları sırasıyla üretir
    ve aralarına pürüzsüz sessizlik kareleri ekleyerek tek bir MP3 dosyasında birleştirir.
    """
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        combined = bytearray()
        ar_count = 0
        tr_count = 0

        for idx, (lang, text, pause_ms) in enumerate(segments):
            if not text.strip():
                continue
            seg_bytes = await _synthesize_segment(lang, text)
            combined.extend(seg_bytes)
            if lang == "ar":
                ar_count += 1
            else:
                tr_count += 1

            # Son parça değilse doğal nefes / geçiş duraklaması ekle
            if pause_ms > 0 and idx < len(segments) - 1:
                frame_count = max(1, round(pause_ms / 24))
                combined.extend(SILENT_MP3_FRAME_24MS * frame_count)

        if len(combined) <= 10_000:
            return False

        tmp_path = out_path.with_suffix(".mp3.tmp")
        tmp_path.write_bytes(bytes(combined))
        tmp_path.replace(out_path)
        print(f"    [TTS] {out_path.name} üretildi (AR: {ar_count} parça, TR: {tr_count} parça, {len(combined)//1024} KB)")
        return True
    except Exception as e:
        print(f"[WARN] AI audio generation failed for {out_path.name}: {e}", file=sys.stderr)
        return False


async def main() -> int:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    existing_by_id: dict[str, dict] = {}
    if OUTPUT_JSON.exists():
        try:
            old_data = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
            for item in old_data.get("hutbeler", []):
                existing_by_id[str(item.get("id"))] = item
        except Exception as e:
            print(f"[WARN] Could not load existing hutbe.json: {e}", file=sys.stderr)

    print("[INFO] Diyanet Din Hizmetleri ana sayfası kontrol ediliyor...")
    try:
        discovered = discover_hutbe_links(limit=MAX_ARCHIVE)
    except Exception as e:
        print(f"[ERROR] Diyanet sayfasına ulaşılamadı, mevcut veri korunuyor: {e}", file=sys.stderr)
        return 0 if existing_by_id else 1

    if not discovered:
        print("[WARN] Hutbe bağlantısı bulunamadı, mevcut veri korunuyor.", file=sys.stderr)
        return 0 if existing_by_id else 1

    results: list[dict] = []
    keep_audio_files: set[str] = set()

    for entry in discovered:
        hid = str(entry["id"])
        audio_filename = f"hutbe_{hid}_v{AUDIO_VERSION}.mp3"
        audio_path = AUDIO_DIR / audio_filename
        ai_audio_url = f"{RAW_BASE_URL}/audio/{audio_filename}"

        cached = existing_by_id.get(hid)
        if (
            cached
            and cached.get("bloklar")
            and cached.get("ses_versiyon") == AUDIO_VERSION
            and audio_path.exists()
            and audio_path.stat().st_size > 10_000
        ):
            cached["ai_ses_url"] = ai_audio_url
            results.append(cached)
            keep_audio_files.add(audio_filename)
            print(f"[SKIP] Zaten güncel (v{AUDIO_VERSION}): #{hid} — {cached.get('baslik')}")
            continue

        print(f"[FETCH] Hutbe işleniyor (v{AUDIO_VERSION}): #{hid} ({entry['date_hint']})")
        try:
            detail_html = fetch_text(entry["url"])
            pdf_url, pdf_title, diyanet_mp3_url = extract_attachments(detail_html)
            if not pdf_url:
                print(f"[WARN] #{hid} için Türkçe PDF bulunamadı, atlanıyor.", file=sys.stderr)
                continue

            pdf_bytes = fetch_bytes(pdf_url)
            parsed = parse_hutbe_pdf(pdf_bytes, pdf_title, entry["date_hint"])
            if not parsed["bloklar"]:
                print(f"[WARN] #{hid} PDF metni boş çıktı, atlanıyor.", file=sys.stderr)
                continue

            # Arapça + Türkçe çift sesli yapay zeka hutbe MP3 üret
            segments = build_tts_segments(
                parsed["baslik"],
                parsed["tarih_uzun"],
                parsed["arapca_giris"],
                parsed["bloklar"],
            )
            ai_ok = await generate_ai_audio(segments, audio_path)

            item_obj = {
                "id": hid,
                "tarih": parsed["tarih"],
                "tarih_uzun": parsed["tarih_uzun"],
                "tarih_iso": parsed["tarih_iso"],
                "baslik": parsed["baslik"],
                "okuma_suresi": parsed["okuma_suresi"],
                "arapca_giris": parsed["arapca_giris"],
                "bloklar": parsed["bloklar"],
                "dipnotlar": parsed["dipnotlar"],
                "ai_ses_url": ai_audio_url if ai_ok else "",
                "diyanet_ses_url": diyanet_mp3_url,
                "kaynak_url": encode_url(entry["url"]),
                "ses_versiyon": AUDIO_VERSION,
            }
            results.append(item_obj)
            if ai_ok:
                keep_audio_files.add(audio_filename)
            print(f"[OK] #{hid} hazır: {parsed['baslik']} (AI ses v{AUDIO_VERSION}: {ai_ok}, {len(parsed['bloklar'])} blok)")
        except Exception as e:
            print(f"[WARN] #{hid} işlenirken hata: {e}", file=sys.stderr)
            if cached:
                results.append(cached)
                old_url_name = Path(cached.get("ai_ses_url", "")).name
                if old_url_name and (AUDIO_DIR / old_url_name).exists():
                    keep_audio_files.add(old_url_name)

    if not results:
        print("[WARN] Hiç geçerli hutbe elde edilemedi, mevcut dosya korunuyor.", file=sys.stderr)
        return 0 if existing_by_id else 1

    results.sort(key=lambda x: (x.get("tarih_iso") or "", int(x.get("id") or 0)), reverse=True)

    # Eski ses dosyalarını (v1 ve arşiv dışı) temizle
    for f in AUDIO_DIR.glob("hutbe_*.mp3"):
        if f.name not in keep_audio_files:
            try:
                f.unlink()
                print(f"[CLEAN] Eski ses dosyası silindi: {f.name}")
            except Exception:
                pass

    payload = {
        "guncelleme_zamani": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kaynak": "T.C. Diyanet İşleri Başkanlığı · Din Hizmetleri Genel Müdürlüğü",
        "ses_motoru": f"TR:{TURKISH_VOICE} + AR:{ARABIC_VOICE} (v{AUDIO_VERSION})",
        "guncel": results[0],
        "hutbeler": results,
    }

    if OUTPUT_JSON.exists():
        try:
            old_payload = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
            if old_payload.get("hutbeler") == payload["hutbeler"]:
                print("[INFO] Hutbelerde değişiklik yok, hutbe.json güncellenmedi.")
                return 0
        except Exception:
            pass

    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] hutbe.json güncellendi ({len(results)} hutbe, en güncel: {results[0]['baslik']}).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
