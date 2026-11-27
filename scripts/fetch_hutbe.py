#!/usr/bin/env python3
"""
Diyanet İşleri Başkanlığı Din Hizmetleri Genel Müdürlüğü sayfasından
haftalık Cuma (ve Bayram) hutbelerini otomatik çeker, temiz JSON formatına dönüştürür
ve Türkçe kısımları Microsoft Neural Türkçe ses (tr-TR-AhmetNeural),
Arapça ayet ve hadisleri ise fasih Arapça Neural ses (ar-SA-HamedNeural) ile
doğal biçimde birleştirerek sesli hutbe MP3 + milisaniye hassasiyetli cümle takip
zamanlamalarını (`zamanlar`) üretir.
"""

import asyncio
import datetime
import html
import io
import json
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

# Ses sürümü yükseltildiğinde mevcut tüm hutbe MP3'leri ve cümle zamanlamaları
# otomatik yeniden üretilir; kullanıcı telefonlarındaki eski önbellek atlanır.
AUDIO_VERSION = 3
TURKISH_VOICE = "tr-TR-AhmetNeural"
ARABIC_VOICE = "ar-SA-HamedNeural"

# ISO/IEC 13818-3 uyumlu MPEG-2 Layer III (24kHz, 48kbps, mono) 24ms saf sessizlik karesi
# (edge-tts'in hem tr-TR-AhmetNeural hem ar-SA-HamedNeural çıktısıyla birebir aynı format)
MP3_FRAME_BYTES = 144
MP3_FRAME_MS = 24.0
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

        # 1. Lafza-i celal kalıplarını onar
        ln = re.sub(r"الل[\u0651\u0670\s]+([َُِ])(?=\s|$|[.:،؛؟…])", r"اللّٰه\1", ln)
        ln = re.sub(r"الل[\u0651\u0670]{2,}(?=\s|$|[.:،؛؟…])", "اللّٰهُ", ln)

        # Şedde yerine boşluk + hareke gelen durumları düzelt
        ln = re.sub(r"(?<=[\u0621-\u064A])\s+([\u064E\u064F\u0650])(?=[\u0621-\u064A])", "\u0651\\1", ln)
        ln = re.sub(r"\bإِن\s+َ\b", "إِنَّ", ln)
        ln = re.sub(r"\bقَل\s+َ(?=\s|$|[.:،؛؟…])", "قَلَّ", ln)
        ln = re.sub(r"\bأَحَ\s+ب\s*َ\b", "أَحَبَّ", ln)

        # 2. Harekelerin önündeki fazladan boşlukları temizle
        ln = re.sub(r"\s+([\u064B-\u065F\u0670\u06D6-\u06ED])", r"\1", ln)

        # 3. Kelimeden kopan ة (ta-i merbuta) ekini birleştir
        ln = re.sub(r"([\u0621-\u064A\u064B-\u065F\u0670])\s+(ة[\u064B-\u065F]*)", r"\1\2", ln)

        # 4. Bazı Diyanet PDF fontlarında tüm üstünler şedde+üstün olarak çıkar (>%28 şedde yoğunluğu)
        ar_letters = len(re.findall(r"[\u0621-\u064A]", ln))
        shaddas = ln.count("\u0651")
        if ar_letters >= 10 and (shaddas / ar_letters) > 0.28:
            ln = ln.replace("\u0651", "")
            ln = re.sub(r"الل[َُِٰ]*ه", "اللّٰه", ln)
            ln = ln.replace("الرَحْم", "الرَّحْم").replace("الرَحِيم", "الرَّحِيم")
            ln = ln.replace("الَذ", "الَّذ").replace("النَاس", "النَّاس")
            ln = ln.replace("ثُمَ", "ثُمَّ").replace("إِنَ", "إِنَّ").replace("اِنَ", "إِنَّ")
            ln = ln.replace("كَاَنَ", "كَأَنَّ").replace("إِيَاكُمْ", "إِيَّاكُمْ")

        # 5. Hareke önündeki ٰ (U+0670) aslında şeddedir
        ln = re.sub(r"\u0670([\u064E\u064F\u0650])", "\u0651\\1", ln)
        ln = re.sub(r"\u0670\u0651", "\u0651", ln)

        # 6. Salavat kalıbı
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


def _arabic_line_to_tts(raw_line: str) -> str:
    """Tek bir Arapça satırı fasih Arapça Neural TTS imlasına çevirir."""
    s = raw_line
    s = s.replace("اللّٰه", "اللَّه")
    s = s.replace("الرَّحْمٰن", "الرَّحْمَن").replace("الرَّحْمٰن", "الرَّحْمَن")
    s = s.replace("الْيَتَامٰى", "الْيَتَامَى")
    s = s.replace("حَيٰوةً", "حَيَاةً").replace("حَيٰوةٌ", "حَيَاةٌ").replace("حَيٰوةٍ", "حَيَاةٍ")
    s = s.replace("صَلٰوة", "صَلَاة").replace("زَكٰوة", "زَكَاة")
    s = s.replace("أُولَٰئِكَ", "أُولَئِكَ")
    s = s.replace("اٰ", "آ")
    s = s.replace("ٰى", "َى")
    s = s.replace("اَ", "أَ").replace("اِ", "إِ").replace("اُ", "أُ")
    s = s.replace("ـ", "")
    s = re.sub(r"[\u06EA\u06EC\u06D6-\u06ED\u06DF-\u06E8]", "", s)
    s = s.replace("\u0670", "ا")
    s = re.sub(r"\.{2,}|…+", " ", s)
    s = re.sub(r"\s*[:،؛]\s*", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip(" .،؛:")


def split_arabic_clauses_with_spans(display_text: str) -> list[tuple[int, int, str, int]]:
    """
    Ekranda gösterilen `display_text` içindeki Arapça satırları mantıksal kıraat parçalarına
    (Besmele, Ayet, "Ve kale Resulullahi...", Hadis) böler ve her parçanın
    `(char_start, char_end, tts_text, pause_after_ms)` bilgisini döner.
    """
    if not display_text:
        return []

    line_spans: list[tuple[int, int, str]] = []
    pos = 0
    for ln in display_text.split("\n"):
        ln_start = pos
        ln_end = pos + len(ln)
        pos = ln_end + 1
        if ln.strip() and has_arabic(ln):
            s_idx = ln_start
            while s_idx < ln_end and display_text[s_idx].isspace():
                s_idx += 1
            e_idx = ln_end
            while e_idx > s_idx and display_text[e_idx - 1].isspace():
                e_idx -= 1
            if e_idx > s_idx:
                line_spans.append((s_idx, e_idx, display_text[s_idx:e_idx]))

    if not line_spans:
        return []

    groups: list[tuple[str, list[tuple[int, int, str]]]] = []
    stage = "start"
    for span in line_spans:
        tts_ln = _arabic_line_to_tts(span[2])
        if not tts_ln or not has_arabic(tts_ln):
            continue
        if stage == "start" and ("بِسْمِ اللَّهِ" in tts_ln or "بسم الله" in tts_ln):
            groups.append(("besmele", [span]))
            stage = "ayet"
        elif re.search(r"قَالَ\s+رَسُولُ", tts_ln):
            groups.append(("salavat", [span]))
            stage = "hadis"
        elif stage == "hadis":
            if groups and groups[-1][0] == "hadis":
                groups[-1][1].append(span)
            else:
                groups.append(("hadis", [span]))
        else:
            if groups and groups[-1][0] == "ayet":
                groups[-1][1].append(span)
            else:
                groups.append(("ayet", [span]))
            stage = "ayet"

    clauses: list[tuple[int, int, str, int]] = []
    for kind, spans in groups:
        c_start = spans[0][0]
        c_end = spans[-1][1]
        tts_parts = [_arabic_line_to_tts(x[2]) for x in spans]
        tts_joined = " ".join(p for p in tts_parts if p).strip()
        if not tts_joined:
            continue
        if kind == "besmele":
            clauses.append((c_start, c_end, tts_joined + ".", 480))
        elif kind == "ayet":
            clauses.append((c_start, c_end, tts_joined + ".", 560))
        elif kind == "salavat":
            clauses.append((c_start, c_end, tts_joined + ":", 320))
        else:
            clauses.append((c_start, c_end, tts_joined + ".", 620))

    return clauses


def split_turkish_sentences_with_spans(text: str) -> list[tuple[int, int, str]]:
    """
    Ekranda gösterilen Türkçe paragrafı `(char_start, char_end, cumle)` olarak böler.
    `Hz.`, `(s.a.s)`, `(a.s.)`, `(r.a.)`, `(c.c.)`, `vb.`, `vs.` gibi kısaltmalardan bölmez.
    """
    spans: list[tuple[int, int, str]] = []
    n = len(text)
    start = 0
    i = 0

    while i < n:
        ch = text[i]
        if ch in ".!?…":
            j = i + 1
            while j < n and text[j] in ".!?…":
                j += 1
            while j < n and text[j] in "\"”’')":
                j += 1
            m_fn = re.match(r"\s*\[\d{1,2}\]", text[j:])
            if m_fn:
                j += m_fn.end()

            prefix = text[max(0, i - 14):i + 1].lower()
            is_abbr = bool(
                re.search(r"(?:\bhz|\bs\.a|\ba\.s|\br\.a|\bc\.c|\bvb|\bvs|\bdr|\bprof|\b\d)\.$", prefix)
                or re.search(r"\([a-z]\.[a-z]\.?[a-z]?\.?$", prefix)
            )
            at_end = j >= n or bool(re.match(r"\s+", text[j:]))
            if not is_abbr and at_end:
                s_idx = start
                while s_idx < j and text[s_idx].isspace():
                    s_idx += 1
                e_idx = j
                while e_idx > s_idx and text[e_idx - 1].isspace():
                    e_idx -= 1
                if e_idx > s_idx:
                    spans.append((s_idx, e_idx, text[s_idx:e_idx]))
                start = j
                i = j
                continue
        i += 1

    if start < n:
        s_idx = start
        while s_idx < n and text[s_idx].isspace():
            s_idx += 1
        e_idx = n
        while e_idx > s_idx and text[e_idx - 1].isspace():
            e_idx -= 1
        if e_idx > s_idx:
            spans.append((s_idx, e_idx, text[s_idx:e_idx]))

    return spans


def clean_turkish_text_for_tts(text: str) -> str:
    """Türkçe hutbe cümlesini akıcı ve doğal okunuş için temizler."""
    t = text
    t = re.sub(r"\[\d{1,2}\]", "", t)
    t = re.sub(r"([.!?\"”’…])\s*\d{1,2}(?=\s|$|,|;)", r"\1", t)
    t = re.sub(r"\(\s*s\.?\s*a\.?\s*[sv]\.?\s*\)", " sallallahu aleyhi ve sellem ", t, flags=re.I)
    t = re.sub(r"\(\s*a\.?\s*s\.?\s*\)", " aleyhisselam ", t, flags=re.I)
    t = re.sub(r"\(\s*r\.?\s*a\.?\s*\)", " radıyallahu anh ", t, flags=re.I)
    t = re.sub(r"\(\s*c\.?\s*c\.?\s*\)", " celle celaluhu ", t, flags=re.I)
    t = re.sub(r"\bHz\.\s*", "Hazreti ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _normalize_alnum_tr(s: str) -> str:
    """Türkçe harf duyarlı yalnızca harf/rakam karşılaştırma anahtarı."""
    s = s.replace("İ", "i").replace("I", "ı").lower()
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


def _align_sentences_to_word_boundaries(
    sentence_spans: list[tuple[int, int, str]],
    tts_sentences: list[str],
    word_boundaries: list[tuple[int, int, str]],
    seg_offset_ms: int,
    seg_duration_ms: int,
    hedef_id: int,
) -> list[dict]:
    """
    Edge TTS `WordBoundary` zaman damgalarını karakter-tüketim yöntemiyle
    her cümlenin ekrandaki `(char_start, char_end)` aralığına milisaniye hassasiyetle eşler.
    """
    if not sentence_spans:
        return []

    if len(sentence_spans) == 1:
        c_start, c_end, _ = sentence_spans[0]
        if word_boundaries:
            t0 = seg_offset_ms + word_boundaries[0][0]
            t1 = seg_offset_ms + word_boundaries[-1][1]
        else:
            t0 = seg_offset_ms
            t1 = seg_offset_ms + seg_duration_ms
        return [{"hedef": hedef_id, "b": c_start, "s": c_end, "t0": int(t0), "t1": int(max(t0 + 150, t1))}]

    if not word_boundaries:
        # Yedek: karakter uzunluğuna orantılı dağıtım
        total_chars = max(1, sum(len(t) for t in tts_sentences))
        cur = float(seg_offset_ms)
        out = []
        for (c_start, c_end, _), tts_s in zip(sentence_spans, tts_sentences):
            dur = seg_duration_ms * (len(tts_s) / total_chars)
            out.append({
                "hedef": hedef_id,
                "b": c_start,
                "s": c_end,
                "t0": int(round(cur)),
                "t1": int(round(cur + dur)),
            })
            cur += dur
        return out

    # Her WordBoundary için harf uzunluğu hesapla ve kümülatif harf ekseninde eşle
    wb_norm_lens = [max(1, len(_normalize_alnum_tr(w[2]))) for w in word_boundaries]
    total_wb_chars = sum(wb_norm_lens)
    sent_norm_lens = [max(1, len(_normalize_alnum_tr(ts))) for ts in tts_sentences]
    total_sent_chars = sum(sent_norm_lens)
    scale = (total_wb_chars / total_sent_chars) if total_sent_chars > 0 else 1.0

    # Kümülatif WB bitişleri
    wb_cum_end: list[int] = []
    acc = 0
    for ln in wb_norm_lens:
        acc += ln
        wb_cum_end.append(acc)

    results: list[dict] = []
    wb_cursor = 0
    sent_cum = 0.0

    for idx, ((c_start, c_end, _), s_len) in enumerate(zip(sentence_spans, sent_norm_lens)):
        sent_cum += s_len * scale
        start_wb = min(wb_cursor, len(word_boundaries) - 1)
        if idx == len(sentence_spans) - 1:
            end_wb = len(word_boundaries) - 1
        else:
            # sent_cum hedefine en yakın WB indeksini bul
            end_wb = start_wb
            while end_wb < len(word_boundaries) - 1 and wb_cum_end[end_wb] < sent_cum - 1.5:
                end_wb += 1
            # Eğer bir sonraki kelime bu cümlenin son kelimesiyle birebir örtüşüyorsa ilerlet
            next_idx = end_wb + 1
            if next_idx < len(word_boundaries):
                sent_words = re.findall(r"[\wçğıöşüÇĞİÖŞÜ]+", tts_sentences[idx])
                last_word_norm = _normalize_alnum_tr(sent_words[-1]) if sent_words else ""
                next_wb_norm = _normalize_alnum_tr(word_boundaries[next_idx][2])
                if last_word_norm and next_wb_norm and (
                    next_wb_norm == last_word_norm or last_word_norm.endswith(next_wb_norm)
                ):
                    end_wb = next_idx

        t0 = seg_offset_ms + word_boundaries[start_wb][0]
        t1 = seg_offset_ms + word_boundaries[end_wb][1]
        if results and t0 < results[-1]["t1"]:
            t0 = results[-1]["t1"]
        results.append({
            "hedef": hedef_id,
            "b": c_start,
            "s": c_end,
            "t0": int(t0),
            "t1": int(max(t0 + 150, t1)),
        })
        wb_cursor = min(end_wb + 1, len(word_boundaries) - 1)

    return results


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

        if not started_body and has_arabic(ln):
            if ln == "﷽":
                arapca_lines.append("بِسْمِ اللّٰهِ الرَّحْمٰنِ الرَّحِيمِ")
            else:
                arapca_lines.append(ln)
            continue

        if not started_body:
            if is_salutation(ln):
                started_body = True
                body_lines.append(ln)
            else:
                title_from_pdf = (title_from_pdf + " " + ln).strip()
            continue

        body_lines.append(ln)

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
    """Detay sayfasından (turkce_pdf_url, turkce_baslik, diyanet_mp3_url) döner."""
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


async def _synthesize_segment_with_timings(
    lang: str,
    text: str,
    retries: int = 3,
) -> tuple[bytes, list[tuple[int, int, str]]]:
    """
    Tek bir Türkçe veya Arapça bölümü uygun Neural AI sesiyle sentezler;
    hem ham MP3 byte'larını hem de `WordBoundary` zaman damgalarını `(start_ms, end_ms, word)` döner.
    """
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
            wbs: list[tuple[int, int, str]] = []
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate,
                pitch=pitch,
                boundary="WordBoundary",
            )
            async for chunk in communicate.stream():
                ctype = chunk["type"]
                if ctype == "audio":
                    chunks.extend(chunk["data"])
                elif ctype == "WordBoundary":
                    st_ms = int(chunk["offset"] // 10_000)
                    en_ms = int((chunk["offset"] + chunk["duration"]) // 10_000)
                    wbs.append((st_ms, en_ms, chunk.get("text", "")))
            if len(chunks) > 500:
                return bytes(chunks), wbs
            raise RuntimeError("Boş ses akışı döndü")
        except Exception as e:
            last_err = e
            print(f"[WARN] TTS ({lang}/{voice}) deneme {attempt}/{retries} başarısız: {e}", file=sys.stderr)
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"TTS segment failed ({lang}): {last_err}")


async def generate_ai_audio_with_timings(
    baslik: str,
    tarih_uzun: str,
    arapca_giris: str,
    bloklar: list[dict],
    out_path: Path,
) -> tuple[bool, list[dict]]:
    """
    Hutbenin tüm bölümlerini sırasıyla sentezler, tek MP3 dosyasında birleştirir ve
    ekrandaki metinle birebir örtüşen milisaniye hassasiyetli `zamanlar` listesini üretir.
    """
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        combined = bytearray()
        zamanlar: list[dict] = []
        ar_count = 0
        tr_count = 0

        def current_cursor_ms() -> int:
            return int(round((len(combined) / MP3_FRAME_BYTES) * MP3_FRAME_MS))

        def append_pause(pause_ms: int):
            if pause_ms > 0:
                frames = max(1, round(pause_ms / MP3_FRAME_MS))
                combined.extend(SILENT_MP3_FRAME_24MS * frames)

        # 1. Türkçe açılış anonsu (ekranda başlık + tarih)
        intro_text = f"{tarih_uzun} Cuma Hutbesi. Konumuz: {baslik}."
        intro_bytes, _ = await _synthesize_segment_with_timings("tr", intro_text)
        combined.extend(intro_bytes)
        tr_count += 1
        append_pause(450)

        # 2. Arapça açılış ayet ve hadisleri (hedef = -1 -> arapcaGiris)
        if arapca_giris:
            for c_start, c_end, ar_tts, pause_ms in split_arabic_clauses_with_spans(arapca_giris):
                seg_start_ms = current_cursor_ms()
                seg_bytes, wbs = await _synthesize_segment_with_timings("ar", ar_tts)
                seg_dur_ms = int(round((len(seg_bytes) / MP3_FRAME_BYTES) * MP3_FRAME_MS))
                combined.extend(seg_bytes)
                ar_count += 1

                t0 = seg_start_ms + (wbs[0][0] if wbs else 0)
                t1 = seg_start_ms + (wbs[-1][1] if wbs else seg_dur_ms)
                zamanlar.append({
                    "hedef": -1,
                    "b": c_start,
                    "s": c_end,
                    "t0": int(t0),
                    "t1": int(max(t0 + 200, t1)),
                })
                append_pause(pause_ms)

        # 3. Hutbe gövde blokları (blok indeksine göre birebir eşlenmiş cümle zamanlamaları)
        for idx, blok in enumerate(bloklar):
            tip = blok.get("tip", "paragraf")
            display_metin = blok.get("metin", "")
            if not display_metin.strip():
                continue

            is_last_block = (idx == len(bloklar) - 1)

            if tip == "arapca":
                clauses = split_arabic_clauses_with_spans(display_metin)
                for c_start, c_end, ar_tts, pause_ms in clauses:
                    seg_start_ms = current_cursor_ms()
                    seg_bytes, wbs = await _synthesize_segment_with_timings("ar", ar_tts)
                    seg_dur_ms = int(round((len(seg_bytes) / MP3_FRAME_BYTES) * MP3_FRAME_MS))
                    combined.extend(seg_bytes)
                    ar_count += 1

                    t0 = seg_start_ms + (wbs[0][0] if wbs else 0)
                    t1 = seg_start_ms + (wbs[-1][1] if wbs else seg_dur_ms)
                    zamanlar.append({
                        "hedef": idx,
                        "b": c_start,
                        "s": c_end,
                        "t0": int(t0),
                        "t1": int(max(t0 + 200, t1)),
                    })
                    if not is_last_block:
                        append_pause(pause_ms)
            elif tip in ("hitap", "paragraf", "alinti"):
                sent_spans = split_turkish_sentences_with_spans(display_metin)
                tts_sentences = [clean_turkish_text_for_tts(s[2]) for s in sent_spans]
                paired = [
                    (sp, ts) for sp, ts in zip(sent_spans, tts_sentences) if ts
                ]
                if not paired:
                    continue
                valid_spans = [p[0] for p in paired]
                valid_tts = [p[1] for p in paired]
                block_tts_text = " ".join(valid_tts)

                seg_start_ms = current_cursor_ms()
                seg_bytes, wbs = await _synthesize_segment_with_timings("tr", block_tts_text)
                seg_dur_ms = int(round((len(seg_bytes) / MP3_FRAME_BYTES) * MP3_FRAME_MS))
                combined.extend(seg_bytes)
                tr_count += 1

                aligned = _align_sentences_to_word_boundaries(
                    sentence_spans=valid_spans,
                    tts_sentences=valid_tts,
                    word_boundaries=wbs,
                    seg_offset_ms=seg_start_ms,
                    seg_duration_ms=seg_dur_ms,
                    hedef_id=idx,
                )
                zamanlar.extend(aligned)

                if not is_last_block:
                    pause_ms = 240 if tip == "hitap" else 340
                    append_pause(pause_ms)

        if len(combined) <= 10_000:
            return False, []

        tmp_path = out_path.with_suffix(".mp3.tmp")
        tmp_path.write_bytes(bytes(combined))
        tmp_path.replace(out_path)
        total_sec = round((len(combined) / MP3_FRAME_BYTES) * MP3_FRAME_MS / 1000.0, 1)
        print(
            f"    [TTS] {out_path.name} üretildi "
            f"(AR: {ar_count}, TR: {tr_count}, {len(zamanlar)} cümle zamanı, {total_sec}s, {len(combined)//1024} KB)"
        )
        return True, zamanlar
    except Exception as e:
        print(f"[WARN] AI audio generation failed for {out_path.name}: {e}", file=sys.stderr)
        return False, []


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
            and cached.get("zamanlar")
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

            ai_ok, zamanlar = await generate_ai_audio_with_timings(
                parsed["baslik"],
                parsed["tarih_uzun"],
                parsed["arapca_giris"],
                parsed["bloklar"],
                audio_path,
            )

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
                "zamanlar": zamanlar if ai_ok else [],
                "ai_ses_url": ai_audio_url if ai_ok else "",
                "diyanet_ses_url": diyanet_mp3_url,
                "kaynak_url": encode_url(entry["url"]),
                "ses_versiyon": AUDIO_VERSION,
            }
            results.append(item_obj)
            if ai_ok:
                keep_audio_files.add(audio_filename)
            print(f"[OK] #{hid} hazır: {parsed['baslik']} (v{AUDIO_VERSION}: {ai_ok}, {len(zamanlar)} takip aralığı)")
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

    # Eski sürüm ve arşiv dışı ses dosyalarını temizle
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
        "ses_motoru": f"TR:{TURKISH_VOICE} + AR:{ARABIC_VOICE} + WordBoundary Sync (v{AUDIO_VERSION})",
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
