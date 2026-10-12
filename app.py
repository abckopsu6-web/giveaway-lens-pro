import os, re, json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = """Kamu adalah analis syarat giveaway Instagram. Analisis caption secara teliti dan jangan mengarang.
Kembalikan JSON valid dengan schema:
{
 "summary": "ringkasan singkat",
 "deadline": "tanggal/jam atau Tidak ditemukan",
 "winner_announcement": "tanggal/jam atau Tidak ditemukan",
 "accounts_to_follow": ["@akun"],
 "hashtags": ["#tag"],
 "requirements": [{"task":"tugas singkat","details":"rincian","priority":"wajib|periksa"}],
 "comment_rules": ["aturan komentar"],
 "eligibility": ["syarat peserta"],
 "disqualification": ["alasan diskualifikasi"],
 "red_flags": ["indikasi risiko atau aturan meragukan"],
 "comment_suggestion": "komentar natural yang mematuhi instruksi yang diketahui",
 "uncertainties": ["hal yang tidak bisa dipastikan dari teks"]
}
Jangan menyatakan follow/like/repost sudah dilakukan. Bedakan aturan eksplisit dengan dugaan. Jika tidak disebutkan, tulis tidak ditemukan atau array kosong.
Jangan membuat komentar yang mengklaim pengalaman pribadi palsu, jangan menyarankan spam, akun ganda, atau manipulasi giveaway."""

def extract_caption(url):
    if not re.match(r"^https?://(www\.)?instagram\.com/(p|reel|tv)/", url, re.I):
        raise ValueError("Masukkan link postingan Instagram publik yang valid (/p/, /reel/, atau /tv/).")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
    r = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"Instagram membatasi pengambilan halaman (HTTP {r.status_code}). Tempel caption manual.")
    soup = BeautifulSoup(r.text, "html.parser")
    meta = soup.find("meta", attrs={"property": "og:description"})
    desc = meta.get("content", "").strip() if meta else ""
    if not desc or len(desc) < 15:
        meta = soup.find("meta", attrs={"name": "description"})
        desc = meta.get("content", "").strip() if meta else ""
    if not desc:
        raise RuntimeError("Caption tidak bisa dibaca dari link ini. Tempel caption manual.")
    return desc

def local_fallback(caption):
    low = caption.lower()
    handles = list(dict.fromkeys(re.findall(r"@[A-Za-z0-9._]+", caption)))
    hashtags = list(dict.fromkeys(re.findall(r"(?<!\\w)#\\w+", caption)))
    tasks = []
    for pattern, label in [
        (r"\\bfollow\\b|ikuti akun|wajib mengikuti", "Follow akun yang diwajibkan"),
        (r"\\blike\\b|sukai postingan", "Like postingan"),
        (r"\\btag\\b|mention|tandai teman", "Tag/mention teman"),
        (r"story|repost|bagikan ke cerita", "Repost/bagikan ke Story"),
        (r"hashtag|#\\w+", "Gunakan hashtag yang diwajibkan"),
        (r"komen|komentar|comment", "Tulis komentar sesuai instruksi"),
        (r"akun.{0,20}(publik|public)|jangan.{0,15}private", "Pastikan akun memenuhi syarat publik"),
        (r"dm\\b|direct message|kirim.{0,15}pesan", "Kirim DM/bukti jika diwajibkan")]:
        if re.search(pattern, low, re.I):
            tasks.append({"task": label, "details": "Terdeteksi dari caption; periksa aturan asli.", "priority": "periksa"})
    date = re.search(r"\\b\\d{1,2}[./-]\\d{1,2}(?:[./-]\\d{2,4})?\\b", caption)
    return {
        "summary": "Analisis dasar lokal. AI belum aktif atau layanan AI gagal.",
        "deadline": date.group(0) if date and re.search(r"deadline|batas akhir|ditutup|berakhir|last entry|until", low) else "Tidak ditemukan",
        "winner_announcement": "Tidak ditemukan", "accounts_to_follow": handles, "hashtags": hashtags,
        "requirements": tasks or [{"task":"Periksa caption lengkap","details":"Aturan tidak dikenali dengan yakin.","priority":"periksa"}],
        "comment_rules": ["Periksa tema, panjang, kata wajib, dan jumlah komentar."] if re.search(r"komen|komentar|comment", low) else [],
        "eligibility": [], "disqualification": [],
        "red_flags": ["Jangan membayar biaya klaim hadiah atau memberikan password/OTP tanpa verifikasi resmi."] if re.search(r"transfer|bayar dulu|password|kata sandi|otp|kode verifikasi", low) else [],
        "comment_suggestion": "Senang bisa ikut meramaikan giveaway ini. Semoga event-nya berjalan lancar dan selamat untuk semua peserta!",
        "uncertainties": ["Analisis lokal menggunakan pencocokan kata, bukan AI penuh."]
    }

def gemini_analyze(caption, url=""):
    if not GEMINI_API_KEY:
        return local_fallback(caption), "local"
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    prompt = SYSTEM_PROMPT + "\n\nAnalisis caption giveaway berikut. URL sumber: " + (url or "tidak ada") + "\n\nCAPTION:\n" + caption + "\n\nKembalikan hanya JSON valid sesuai schema."
    response = requests.post(
        endpoint,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={"contents":[{"parts":[{"text":prompt}]}],
              "generationConfig":{"temperature":0.2,"responseMimeType":"application/json"}},
        timeout=45
    )
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text), "gemini"

@app.get("/")
def home():
    return render_template("index.html")

@app.post("/api/analyze")
def analyze():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    caption = (data.get("caption") or "").strip()
    if not caption and not url:
        return jsonify(error="Tempel link Instagram atau caption terlebih dahulu."), 400
    source = "caption"
    if not caption and url:
        try:
            caption = extract_caption(url)
            source = "instagram_link"
        except Exception as e:
            return jsonify(error=str(e), fallback_required=True), 422
    try:
        result, mode = gemini_analyze(caption, url)
        return jsonify(result=result, mode=mode, source=source, analyzed_at=datetime.utcnow().isoformat()+"Z")
    except Exception:
        return jsonify(result=local_fallback(caption), mode="local_fallback",
                       warning="Gemini gagal dihubungi atau model/API key tidak cocok. Periksa pengaturan server; hasil ini memakai analisis dasar."), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
