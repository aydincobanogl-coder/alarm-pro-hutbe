#!/usr/bin/env python3
"""
Diyanet İşleri Başkanlığı Din Hizmetleri Genel Müdürlüğü sayfasından
haftalık Cuma (ve Bayram) hutbelerini otomatik çeker, temiz JSON formatına dönüştürür
ve Microsoft Neural Yapay Zeka Sesi (tr-TR-AhmetNeural) ile doğal sesli hutbe MP3 üretir.
"""

import asyncio
import datetime
import html
import io
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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
    # Re-encode path cleanly without double-encoding already encoded sequences
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
    # Noktalama veya tırnak hemen ardındaki 1-2 haneli dipnot numarasını [n] yap
    text = re.sub(r"([.!?\"”’…])\s*(\d{1,2})(?=\s|$|,|;)", r"\1 [\2]", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def clean_for_tts(baslik: str, tarih_uzun: str, bloklar: list[dict]) -> str:
    """Yapay zeka seslendirmesi (edge-tts) için akıcı, doğal Türkçe metin hazırlar."""
    parts = [f"{tarih_uzun} Cuma Hutbesi. Konumuz: {baslik}."]
    for b in bloklar:
        if b["tip"] in ("hitap", "paragraf", "alinti"):
            t = b["metin"]
            # Dipnot referanslarını [1], [2] veya bitişik rakamları temizle
            t = re.sub(r"\[\d{1,2}\]", "", t)
            t = re.sub(r"([.!?\"”’…])\s*\d{1,2}(?=\s|$|,|;)", r"\1", t)
            # Dini kısaltmaları saygılı ve doğal okunuşuna aç
            t = re.sub(r"\(\s*s\.?\s*a\.?\s*[sv]\.?\s*\)", " sallallahu aleyhi ve sellem ", t, flags=re.I)
            t = re.sub(r"\(\s*a\.?\s*s\.?\s*\)", " aleyhisselam ", t, flags=re.I)
            t = re.sub(r"\(\s*r\.?\s*a\.?\s*\)", " radıyallahu anh ", t, flags=re.I)
            t = re.sub(r"\(\s*c\.?\s*c\.?\s*\)", " celle celaluhu ", t, flags=re.I)
            t = re.sub(r"\bHz\.\s*", "Hazreti ", t)
            t = re.sub(r"\s{2,}", " ", t).strip()
            if t:
                parts.append(t)
    return "\n\n".join(parts)


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
            # Çok satırlı dipnot devamı
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
                # PDF'deki başlık (çok satırlı olabilir)
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
            # Eğer paragrafın neredeyse tamamı bir ayet/hadis alıntısıysa veya sadece tırnaklı cümleyse
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
            bloklar.append({"tip": "arapca", "metin": ln})
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
        "arapca_giris": "\n".join(arapca_lines).strip(),
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
        # Tarih çıkarma (örn: 27112026-cuma-hutbesi-yetim...)
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
    Diyanet detay sayfalarında ekler daima göreli '/Documents/...' bağlantısı olarak verilir.
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
            # Arapça PDF atlanır; ilk Türkçe PDF alınır
            if not has_arabic(label) and not has_arabic(href):
                pdf_url = encode_url(BASE_URL + href)
                pdf_title = label

    return pdf_url, pdf_title, mp3_url


async def generate_ai_audio(script_text: str, out_path: Path) -> bool:
    """Microsoft Neural AI Türkçe ses (tr-TR-AhmetNeural) ile akıcı MP3 üretir."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        communicate = edge_tts.Communicate(
            text=script_text,
            voice="tr-TR-AhmetNeural",
            rate="-5%",
            pitch="-2Hz",
        )
        await communicate.save(str(out_path))
        return out_path.exists() and out_path.stat().st_size > 10_000
    except Exception as e:
        print(f"[WARN] AI audio generation failed for {out_path.name}: {e}", file=sys.stderr)
        return False


async def main() -> int:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    # Mevcut hutbe.json varsa önbellek olarak yükle (hata anında veri kaybetmemek için)
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
        audio_filename = f"hutbe_{hid}.mp3"
        audio_path = AUDIO_DIR / audio_filename
        ai_audio_url = f"{RAW_BASE_URL}/audio/{audio_filename}"

        # Eğer daha önce eksiksiz işlendiyse ve MP3 mevcutsa doğrudan yeniden kullan
        cached = existing_by_id.get(hid)
        if (
            cached
            and cached.get("bloklar")
            and audio_path.exists()
            and audio_path.stat().st_size > 10_000
        ):
            cached["ai_ses_url"] = ai_audio_url
            results.append(cached)
            keep_audio_files.add(audio_filename)
            print(f"[SKIP] Zaten güncel: #{hid} — {cached.get('baslik')}")
            continue

        print(f"[FETCH] Yeni hutbe işleniyor: #{hid} ({entry['date_hint']})")
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

            # Yapay zeka sesli hutbe üret
            tts_script = clean_for_tts(parsed["baslik"], parsed["tarih_uzun"], parsed["bloklar"])
            ai_ok = False
            if audio_path.exists() and audio_path.stat().st_size > 10_000:
                ai_ok = True
            else:
                ai_ok = await generate_ai_audio(tts_script, audio_path)

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
            }
            results.append(item_obj)
            if ai_ok:
                keep_audio_files.add(audio_filename)
            print(f"[OK] #{hid} hazır: {parsed['baslik']} (AI ses: {ai_ok}, {len(parsed['bloklar'])} blok)")
        except Exception as e:
            print(f"[WARN] #{hid} işlenirken hata: {e}", file=sys.stderr)
            if cached:
                results.append(cached)
                if audio_path.exists():
                    keep_audio_files.add(audio_filename)

    if not results:
        print("[WARN] Hiç geçerli hutbe elde edilemedi, mevcut dosya korunuyor.", file=sys.stderr)
        return 0 if existing_by_id else 1

    # Tarihe göre en yeniden eskiye sırala
    results.sort(key=lambda x: (x.get("tarih_iso") or "", int(x.get("id") or 0)), reverse=True)

    # Eski ses dosyalarını temizle (depo boyutunu küçük tutmak için)
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
        "guncel": results[0],
        "hutbeler": results,
    }

    # Sadece gerçek içerik değiştiyse yaz (gereksiz boş commitleri engelle)
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
