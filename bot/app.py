#!/usr/bin/env python3
"""
Flask application for Telegram monitoring bot with Grafana/Prometheus integration.
"""

import json
import os
from flask import Flask, request, jsonify
import requests
from datetime import datetime

app = Flask(__name__)

# === 1. Конфигурация ===

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
cfg_path = os.path.join(BASE_DIR, "config.json")

# Попробуем загрузить конфиг, если он существует
CONFIG = {}
if os.path.exists(cfg_path):
    with open(cfg_path) as f:
        CONFIG = json.load(f)

# === 2. Основные настройки ===

# 🔧 Укажи свой токен (либо в config.json, либо напрямую)
BOT_TOKEN = CONFIG.get("BOT_TOKEN", "8123686383:AAFrvTZeU9riIEoFVht0VwwlKKFyKfJSKuo")  # <-- вставь сюда свой токен
PROMETHEUS_URL = CONFIG.get("PROMETHEUS_URL", "http://localhost:9090")
ADMIN_CHAT_ID = CONFIG.get("ADMIN_CHAT_ID", "632306300")  # <-- вставь chat_id или оставь None

SUBSCR_FILE = os.path.join(BASE_DIR, "subscribers.json")

# === 3. Работа с подписчиками ===
def load_subscribers():
    try:
        with open(SUBSCR_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_subscribers(lst):
    with open(SUBSCR_FILE, "w") as f:
        json.dump(lst, f)

# === 4. Отправка сообщений в Telegram ===
def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        app.logger.error("Failed to send message: %s", e)
        return False

# === 5. Запросы к Prometheus ===
def prom_query(query):
    url = f"{PROMETHEUS_URL}/api/v1/query"
    try:
        r = requests.get(url, params={"query": query}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data["status"] != "success":
            return None
        results = data["data"]["result"]
        if not results:
            return None
        val = results[0]["value"][1]
        return float(val)
    except Exception as e:
        app.logger.error("Prometheus query failed: %s", e)
        return None

# === 6. Формирование текста для /status ===
def build_status_text():
    q_cpu = '100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
    q_mem = '(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100'
    q_disk = '(node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100'
    q_net_rx = 'rate(node_network_receive_bytes_total[5m]) * 8 / 1e6'
    q_net_tx = 'rate(node_network_transmit_bytes_total[5m]) * 8 / 1e6'

    cpu = prom_query(q_cpu)
    mem = prom_query(q_mem)
    disk = prom_query(q_disk)
    rx = prom_query(q_net_rx)
    tx = prom_query(q_net_tx)

    lines = [f"*Status report — {datetime.utcnow().isoformat()} UTC*"]
    lines.append(f"CPU: `{cpu:.1f}%`" if cpu else "CPU: `N/A`")
    lines.append(f"Memory used: `{mem:.1f}%`" if mem else "Memory: `N/A`")
    lines.append(f"Disk free (root): `{disk:.1f}%`" if disk else "Disk: `N/A`")
    lines.append(
        f"Network RX/TX: `{rx:.2f}` Mbps / `{tx:.2f}` Mbps" if (rx and tx) else "Network: `N/A`"
    )
    return "\n".join(lines)

# === 7. Обработчик Telegram webhook ===
@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    message = data.get("message") or data.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/status"):
        status = build_status_text()
        send_telegram(chat_id, status)
        return jsonify({"ok": True})

    if text.startswith("/start"):
        send_telegram(chat_id, "Привет! Я бот мониторинга. Ты подписан на оповещения.")
        subs = load_subscribers()
        if chat_id not in subs:
            subs.append(chat_id)
            save_subscribers(subs)
        return jsonify({"ok": True})

    if text.startswith("/stop"):
        subs = load_subscribers()
        if chat_id in subs:
            subs.remove(chat_id)
            save_subscribers(subs)
        send_telegram(chat_id, "Отключил оповещения для этого чата.")
        return jsonify({"ok": True})

    send_telegram(chat_id, "Команда не распознана. Используйте /status или /start.")
    return jsonify({"ok": True})

# === 8. Обработчик Grafana/Alertmanager webhook ===
@app.route("/grafana_webhook", methods=["POST"])
def grafana_webhook():
    payload = request.get_json(force=True)
    if payload is None:
        return jsonify({"ok": False}), 400

    text = "[ALERT] Received notification\n"
    if payload.get("alerts"):
        for a in payload["alerts"]:
            status = a.get("status", "")
            labels = a.get("labels", {})
            annotations = a.get("annotations", {})
            name = labels.get("alertname", "alert")
            desc = annotations.get("description") or annotations.get("summary") or ""
            inst = labels.get("instance") or labels.get("host") or ""
            text += f"\n*{name}* — {status}\nInstance: `{inst}`\n{desc}\n"
    else:
        title = payload.get("title") or payload.get("ruleName") or "Grafana alert"
        state = payload.get("state") or payload.get("status") or ""
        message = payload.get("message") or ""
        text += f"\n*{title}* — `{state}`\n{message}\n"

    subs = load_subscribers()
    if not subs and ADMIN_CHAT_ID:
        subs = [ADMIN_CHAT_ID]

    for c in subs:
        send_telegram(c, text)

    return jsonify({"ok": True})

# === 9. Установка Telegram webhook ===
@app.route("/set_telegram_webhook", methods=["POST"])
def set_webhook():
    data = request.get_json(force=True)
    url = data.get("url")
    if not url:
        return jsonify({"error": "url required"}), 400
    set_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    r = requests.post(set_url, json={"url": url}, timeout=10)
    return jsonify(r.json())

@app.route("/", methods=["GET"])
def index():
    return "Monitoring Telegram webhook service is running."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
