from flask import Flask, request, jsonify
from flask_cors import CORS
import os, ssl, smtplib, time, json, atexit, threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from collections import deque

# ------------------ Config ------------------
load_dotenv()
app = Flask(__name__)
CORS(app)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.getenv("GMAIL_USER")
SENDER_PASS  = os.getenv("GMAIL_APP_PASS")

API_KEY = os.getenv("API_KEY")

# Persistencia
DATA_FILE = os.getenv("DATA_FILE", "telemetry.json")   # ruta del archivo
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "2000"))    # tamaño de buffer en memoria
AUTOSAVE_SEC = int(os.getenv("AUTOSAVE_SEC", "10"))    # cada cuántos segundos guardar

TELEM_HISTORY = deque(maxlen=MAX_HISTORY)
_history_lock = threading.Lock()
_stop_autosave = threading.Event()

# ------------------ Utilidades persistencia ------------------
def load_history():
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Validación básica
        if isinstance(data, list):
            with _history_lock:
                TELEM_HISTORY.clear()
                for item in data[-MAX_HISTORY:]:
                    # filtra mínimos campos válidos
                    if all(k in item for k in ("distance", "battery", "lat", "lng", "t")):
                        TELEM_HISTORY.append(item)
        print(f"[persistencia] Cargadas {len(TELEM_HISTORY)} lecturas desde {DATA_FILE}")
    except Exception as e:
        print(f"[persistencia] No se pudo cargar {DATA_FILE}: {e}")

def save_history():
    try:
        with _history_lock:
            data = list(TELEM_HISTORY)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
        # print(f"[persistencia] Guardadas {len(data)} lecturas en {DATA_FILE}")
    except Exception as e:
        print(f"[persistencia] Error guardando {DATA_FILE}: {e}")

def autosave_loop():
    while not _stop_autosave.is_set():
        # espera con posibilidad de detener
        if _stop_autosave.wait(AUTOSAVE_SEC):
            break
        save_history()

# Cargar al inicio y arrancar autosave
load_history()
_autosave_thread = threading.Thread(target=autosave_loop, daemon=True)
_autosave_thread.start()

# Guardar al terminar el proceso
@atexit.register
def on_exit():
    _stop_autosave.set()
    try:
        _autosave_thread.join(timeout=2)
    except Exception:
        pass
    save_history()

# ------------------ Rutas ------------------
@app.route("/health")
def health():
    return {"ok": True, "history": len(TELEM_HISTORY)}

@app.route("/api/emergency", methods=["POST"])
def emergency():
    if not SENDER_EMAIL or not SENDER_PASS:
        return jsonify({"ok": False, "error": "Servidor sin credenciales (GMAIL_USER/GMAIL_APP_PASS)"}), 500

    data = request.get_json(force=True)
    to_raw  = data.get("to", "").strip()
    subject = data.get("subject", "Alerta de emergencia")
    body    = data.get("body", "Se presionó el botón de emergencia.")

    if not to_raw:
        return jsonify({"ok": False, "error": "Falta destinatario ('to')"}), 400

    tos = [x.strip() for x in to_raw.split(",") if x.strip()]
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = ", ".join(tos)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(SENDER_EMAIL, SENDER_PASS)
            server.sendmail(SENDER_EMAIL, tos, msg.as_string())

        return jsonify({"ok": True, "message": "Correo enviado"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/telemetry", methods=["POST"])
def telemetry():
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    try:
        distance = float(data.get("distance"))
        battery  = float(data.get("battery"))
        lat      = float(data.get("lat"))
        lng      = float(data.get("lng"))
    except Exception:
        return jsonify({"ok": False, "error": "Bad payload"}), 400

    t = data.get("t") or time.time()
    item = {
        "distance": round(distance, 2),
        "battery":  round(battery, 2),
        "lat":      round(lat, 6),
        "lng":      round(lng, 6),
        "t":        t
    }
    with _history_lock:
        TELEM_HISTORY.append(item)

    # Guardado inmediato opcional (rápido y seguro por el reemplazo atómico)
    # save_history()

    return jsonify({"ok": True})

@app.route("/api/telemetry/latest", methods=["GET"])
def telemetry_latest():
    with _history_lock:
        if not TELEM_HISTORY:
            return jsonify({"ok": True, "data": None})
        return jsonify({"ok": True, "data": TELEM_HISTORY[-1]})

@app.route("/api/telemetry/history", methods=["GET"])
def telemetry_history():
    with _history_lock:
        return jsonify({"ok": True, "data": list(TELEM_HISTORY)})

# ------------------ Main ------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)  # <— importante host 0.0.0.0