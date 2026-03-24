import json, hashlib, uuid, random, time, requests, threading, os, base64
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config["SECRET_KEY"] = "fixart-secret-2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── Global job store ──────────────────────────────────────────────────────────
jobs = {}   # job_id -> { status, logs, result_url, prompt, created_at, image_name }
jobs_lock = threading.Lock()

# ── ENCRYPTION ────────────────────────────────────────────────────────────────
_AES_KEY = b"e82ckenh8dichen8"

def qo(endpoint: str, data: dict) -> str:
    n = json.dumps(data, separators=(",", ":"))
    r = "nobody" + endpoint + "use" + n + "md5forencrypt"
    a = hashlib.md5(r.encode()).hexdigest()
    o = endpoint + "-36cd479b6b5-" + n + "-36cd479b6b5-" + a
    cipher = AES.new(_AES_KEY, AES.MODE_ECB)
    ct = cipher.encrypt(pad(o.encode(), 16))
    return ct.hex().upper()

# ── PROXY ─────────────────────────────────────────────────────────────────────
PROXYSCRAPE_URL = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=display_proxies&proxy_format=protocolipport&format=text"
)

def fetch_proxies():
    try:
        r = requests.get(PROXYSCRAPE_URL, timeout=10)
        proxies = [line.strip() for line in r.text.splitlines() if line.strip()]
        random.shuffle(proxies)
        return proxies
    except Exception as e:
        return []

def test_proxy(proxy_url, test_url="https://backend.fixart.ai", timeout=5):
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        r = requests.get(test_url, proxies=proxies, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False

def find_working_proxy(job_id, max_workers=30):
    import queue as _queue
    proxy_list = fetch_proxies()
    log(job_id, f"info", f"Proxy listesi çekildi — {len(proxy_list)} adet taranacak")
    if not proxy_list:
        return None

    result_q     = _queue.Queue()
    found_event  = threading.Event()
    counter_lock = threading.Lock()
    tested_count = [0]
    total        = len(proxy_list)

    def probe(proxy):
        if found_event.is_set():
            return
        ok = test_proxy(proxy)
        with counter_lock:
            tested_count[0] += 1
            idx  = tested_count[0]
            last = (idx == total)

        if ok and not found_event.is_set():
            found_event.set()
            result_q.put(proxy)
            log(job_id, "success", f"Çalışan proxy bulundu [{idx}/{total}]: {proxy}")
        else:
            log(job_id, "muted", f"[{idx}/{total}] ✗ {proxy}")
            if last:
                result_q.put(None)

    log(job_id, "info", f"Paralel proxy taraması başlıyor ({max_workers} thread)...")
    executor = ThreadPoolExecutor(max_workers=max_workers)
    executor.map(lambda p: probe(p), proxy_list)
    working = result_q.get()
    found_event.set()
    executor.shutdown(wait=False, cancel_futures=True)

    if not working:
        log(job_id, "warn", "Çalışan proxy bulunamadı, proxysiz devam edilecek.")
    return working

def make_session(proxy_url=None):
    s = requests.Session()
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s

# ── FINGERPRINT ───────────────────────────────────────────────────────────────
CHROME_VERSIONS = [str(v) for v in range(120, 147)]
OS_LIST = [
    ("Windows NT 10.0; Win64; x64", "Windows"),
    ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
    ("X11; Linux x86_64", "Linux"),
]
LANGUAGES = [
    ["tr-TR","tr","en-US","en"], ["en-US","en"], ["de-DE","de","en-US","en"],
]
TIMEZONES = [
    ("Europe/Istanbul",-180), ("Europe/London",0), ("America/New_York",300),
]

def random_fingerprint():
    cv = random.choice(CHROME_VERSIONS)
    os_str, platform = random.choice(OS_LIST)
    langs = random.choice(LANGUAGES)
    tz, tz_offset = random.choice(TIMEZONES)
    ua = (f"Mozilla/5.0 ({os_str}) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{cv}.0.0.0 Safari/537.36")
    now = datetime.now(timezone(timedelta(minutes=-tz_offset)))
    days   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    time_str = (f"{days[now.weekday()]} {months[now.month-1]} {now.day:02d} {now.year} "
                f"{now.strftime('%H:%M:%S')} GMT{now.strftime('%z')} ({tz})")
    browser_info = {"language":langs[0],"languages":langs,"timeZone":tz,
                    "timezoneOffset":tz_offset,"userAgent":ua,"timeString":time_str}
    ga_cid = f"GA1.1.{random.randint(100000000,999999999)}.{random.randint(1700000000,1800000000)}"
    sec_ch_ua = f'"Chromium";v="{cv}", "Not-A.Brand";v="24", "Google Chrome";v="{cv}"'
    return {
        "ua": ua, "platform": platform,
        "browser_info": quote(json.dumps(browser_info, separators=(",",":"))),
        "ga_cid": ga_cid, "sec_ch_ua": sec_ch_ua,
    }

# ── LOGGING ───────────────────────────────────────────────────────────────────
def log(job_id, level, message):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    entry = {"ts": ts, "level": level, "msg": message}
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["logs"].append(entry)
    socketio.emit("log", {"job_id": job_id, **entry}, room=f"job_{job_id}")

def set_status(job_id, status, extra=None):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = status
            if extra:
                jobs[job_id].update(extra)
    payload = {"job_id": job_id, "status": status}
    if extra:
        payload.update(extra)
    socketio.emit("status", payload, room=f"job_{job_id}")
    socketio.emit("jobs_update", get_jobs_summary())

# ── CORE JOB RUNNER ───────────────────────────────────────────────────────────
ENDPOINT       = "/v2/user/register"
ENDPOINT_URL   = "/api" + ENDPOINT
POLLO_ENDPOINT = "/tools/video/customPollo"
POLLO_ENDPOINT_URL = "/api" + POLLO_ENDPOINT

def run_job(job_id, image_bytes, image_name, prompt, max_workers, video_length, resolution):
    set_status(job_id, "running")
    log(job_id, "info", f"İş başlatıldı | Prompt: '{prompt}' | {video_length} | {resolution}")

    direct_session = make_session(None)
    attempt = 0

    while True:
        attempt += 1
        log(job_id, "info", f"── Deneme #{attempt} ──")

        with jobs_lock:
            if jobs[job_id].get("cancelled"):
                log(job_id, "warn", "İş iptal edildi.")
                set_status(job_id, "cancelled")
                return

        working_proxy   = find_working_proxy(job_id, max_workers=max_workers)
        proxied_session = make_session(working_proxy)

        fp           = random_fingerprint()
        visitor_uuid = uuid.uuid4().hex

        params_value = qo(ENDPOINT, {
            "uuid": visitor_uuid, "endpoint_type": "web", "subscribe_type": "0",
        })

        BASE_HEADERS = {
            "accept":             "application/json, text/plain, */*",
            "accept-language":    "en",
            "browser-info":       fp["browser_info"],
            "gaclientid":         fp["ga_cid"],
            "referer":            "https://fixart.ai/",
            "sec-ch-ua":          fp["sec_ch_ua"],
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": f'"{fp["platform"]}"',
            "token":              "",
            "user-agent":         fp["ua"],
        }

        reg_headers = {
            **BASE_HEADERS,
            "authority":       "backend.fixart.ai",
            "accept-encoding": "gzip, deflate, br, zstd",
            "content-type":    "application/json",
            "origin":          "https://fixart.ai",
            "priority":        "u=1, i",
            "sec-fetch-dest":  "empty",
            "sec-fetch-mode":  "cors",
            "sec-fetch-site":  "same-site",
        }

        try:
            log(job_id, "info", "Register isteği gönderiliyor...")
            reg_resp = proxied_session.post(
                "https://backend.fixart.ai" + ENDPOINT_URL,
                headers=reg_headers,
                json={"params": params_value},
            )
            reg_json = reg_resp.json()
        except Exception as e:
            log(job_id, "error", f"Register başarısız: {e} — yeniden deneniyor...")
            continue

        log(job_id, "info", f"Register → {reg_resp.status_code} | code={reg_json.get('code')}")

        if reg_json.get("code", -1) < 0:
            log(job_id, "error", f"Register hatası: {reg_json.get('msg')} — yeniden deneniyor...")
            continue

        v_token = reg_json["data"]["vToken"]
        log(job_id, "success", f"Token alındı: {v_token[:20]}...")

        pollo_headers = {
            **BASE_HEADERS,
            "vtoken":          v_token,
            "origin":          "https://fixart.ai",
            "sec-fetch-dest":  "empty",
            "sec-fetch-mode":  "cors",
            "sec-fetch-site":  "same-site",
        }

        pollo_params = qo(POLLO_ENDPOINT, {
            "name": "Fixart 2.0",
            "options": {
                "prompt":           prompt,
                "length":           video_length,
                "resolution":       resolution,
                "publicVisibility": "0",
                "audio":            "FALSE",
            },
        })

        try:
            log(job_id, "info", "Video isteği gönderiliyor (Pollo)...")
            ext = os.path.splitext(image_name)[1].lower()
            mime = "image/jpeg" if ext in (".jpg",".jpeg") else "image/png" if ext == ".png" else "image/webp"
            files = {"image": (image_name, image_bytes, mime)}
            data  = {"params": pollo_params}
            pollo_resp = proxied_session.post(
                "https://backend.fixart.ai" + POLLO_ENDPOINT_URL,
                headers=pollo_headers,
                files=files,
                data=data,
            )
            pollo_json = pollo_resp.json()
        except Exception as e:
            log(job_id, "error", f"Pollo isteği başarısız: {e} — yeniden deneniyor...")
            continue

        log(job_id, "info", f"Pollo → {pollo_resp.status_code} | code={pollo_json.get('code')}")

        if pollo_json.get("code", -1) < 0:
            log(job_id, "error", f"Pollo hatası: {pollo_json.get('msg')} — yeniden deneniyor...")
            continue

        job_api_id = pollo_json["data"]["job_id"]
        log(job_id, "success", f"Job ID alındı: {job_api_id}")
        set_status(job_id, "polling", {"api_job_id": job_api_id})
        break

    # ── Polling ───────────────────────────────────────────────────────────────
    query_headers = {
        **BASE_HEADERS,
        "vtoken":         v_token,
        "origin":         "https://fixart.ai",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }

    log(job_id, "info", "Polling başladı (proxysiz)...")
    while True:
        with jobs_lock:
            if jobs[job_id].get("cancelled"):
                log(job_id, "warn", "İş iptal edildi (polling sırasında).")
                set_status(job_id, "cancelled")
                return
        try:
            qr = direct_session.get(
                "https://backend.fixart.ai/api/tools/job/queryV1",
                headers=query_headers,
                params={"job_id": job_api_id},
            )
            result     = qr.json()
            jp         = result["data"]["job_process"]
            is_done    = jp.get("is_completed", False)
            status_str = jp.get("status", "")
            progress   = jp.get("progress", 0)
            log(job_id, "info", f"Durum: {status_str} | İlerleme: {progress}% | Tamamlandı: {is_done}")

            if is_done and status_str == "success":
                video_url = result["data"]["info"]["output_resource"]
                log(job_id, "success", f"Video hazır! URL: {video_url}")
                set_status(job_id, "done", {"result_url": video_url})
                return
            elif status_str == "failed" or result["data"].get("exception"):
                log(job_id, "error", f"Video üretimi başarısız: {result}")
                set_status(job_id, "failed")
                return
        except Exception as e:
            log(job_id, "error", f"Polling hatası: {e}")

        delay = jp.get("next_delay", 5000) if 'jp' in dir() else 5000
        if delay < 0:
            delay = 5000
        time.sleep(delay / 1000)

# ── HELPERS ───────────────────────────────────────────────────────────────────
def get_jobs_summary():
    with jobs_lock:
        return [
            {
                "id":         jid,
                "status":     j["status"],
                "prompt":     j["prompt"],
                "image_name": j["image_name"],
                "created_at": j["created_at"],
                "result_url": j.get("result_url"),
            }
            for jid, j in jobs.items()
        ]

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/start", methods=["POST"])
def start_job():
    image_file   = request.files.get("image")
    prompt       = request.form.get("prompt", "")
    max_workers  = int(request.form.get("max_workers", 30))
    video_length = request.form.get("video_length", "6s")
    resolution   = request.form.get("resolution", "512p")

    if not image_file:
        return jsonify({"error": "Görsel gerekli"}), 400

    image_bytes = image_file.read()
    image_name  = image_file.filename
    job_id      = uuid.uuid4().hex[:10]

    with jobs_lock:
        jobs[job_id] = {
            "status":     "queued",
            "logs":       [],
            "result_url": None,
            "prompt":     prompt,
            "image_name": image_name,
            "created_at": datetime.now().strftime("%H:%M:%S"),
            "cancelled":  False,
        }

    thread = threading.Thread(
        target=run_job,
        args=(job_id, image_bytes, image_name, prompt, max_workers, video_length, resolution),
        daemon=True
    )
    thread.start()

    socketio.emit("jobs_update", get_jobs_summary())
    return jsonify({"job_id": job_id})

@app.route("/api/jobs")
def list_jobs():
    return jsonify(get_jobs_summary())

@app.route("/api/job/<job_id>")
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Bulunamadı"}), 404
    return jsonify(job)

@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["cancelled"] = True
    return jsonify({"ok": True})

@app.route("/api/job/<job_id>/delete", methods=["DELETE"])
def delete_job(job_id):
    with jobs_lock:
        jobs.pop(job_id, None)
    socketio.emit("jobs_update", get_jobs_summary())
    return jsonify({"ok": True})

# ── SOCKETIO ──────────────────────────────────────────────────────────────────
@socketio.on("subscribe")
def on_subscribe(data):
    job_id = data.get("job_id")
    if job_id:
        join_room(f"job_{job_id}")
        with jobs_lock:
            job = jobs.get(job_id, {})
        # replay existing logs
        for entry in job.get("logs", []):
            emit("log", {"job_id": job_id, **entry})

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
