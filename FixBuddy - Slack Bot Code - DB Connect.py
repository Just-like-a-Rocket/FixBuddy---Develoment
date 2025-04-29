#!/usr/bin/env python3
# FixBuddy – Slack bot + respuestas fijas + KB + DM de notificación
# 2025-04-25

import os, re, io, logging, difflib, shutil
from pathlib import Path
from typing import Optional, List

import pandas as pd
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from langdetect import detect, LangDetectException
from PIL import Image
import pytesseract

# ── 0 · Config ────────────────────────────────────────────────
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SA_CREDS_PATH   = os.getenv("GOOGLE_SA_CREDS", "service_account.json")
GSHEET_ID       = os.getenv("GSHEET_ID")
SHEET_NAME      = os.getenv("SHEET_NAME", "KB")
TEMP_DIR        = Path(os.getenv("TEMP_DIR", "tmp"))
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-8s | %(message)s")

# ── 1 · Helpers para menciones y notificación ─────────────────
DEFAULT_USER_IDS = {          # ← ajusta a tu conveniencia
    "alex":      "D07KG19EDHN",
    "ana":       "D08PN9Z42LS",
    "francisco": "D06KQK7GXME",
    "montse":    "D06FENAK5CZ",
    "martin":    "D08J2FXTW86",
}
USER_IDS = DEFAULT_USER_IDS | {}

def uid(name: str) -> str:
    """Devuelve <@U123> para mencionar o el texto plano si no existe."""
    sid = USER_IDS.get(name.lower())
    return f"<@{sid}>" if sid else name

def dm_notify(responsibles: list[str], channel_id: str, thread_ts: str):
    """Envía DM a cada responsable con el enlace al hilo."""
    link = f"https://slack.com/app_redirect?channel={channel_id}&message_ts={thread_ts}"
    for u in responsibles:
        try:
            client.chat_postMessage(
                channel=u,
                text=f"👀 *Heads-up!* Hay una nueva consulta en <#{channel_id}> → {link}"
            )
        except Exception as e:
            logging.error("No pude avisar a %s: %s", u, e)

# ── 2 · Respuestas fijas (patrones + responsables) ────────────
EN_RENEWALS = [r"\brenewals?\b", r"\bchange request\b", r"\baccounts?\b",
               r"\bopportunit(?:y|ies)\b", r"\brates?\b"]
ES_RENEWALS = [r"\brenovaciones?\b", r"\bsolicitud(?:es)? de cambio\b",
               r"\bcuentas?\b", r"\boportunidades?\b", r"\btarifas?\b"]
EN_FLOW = [r"\bflow errors?\b", r"\bvalidation rules?\b", r"\bbugs?\b"]
ES_FLOW = [r"\berrores? de flujo\b", r"\breglas? de validación\b",
           r"\bvalidación(?:es)?\b", r"\bfallos?\b"]

SUGGESTION = ("Please upload a screenshot of the error, the record link, "
              "or both — whatever you have handy!")

SUCCESS_RENEWAL = ("Hi, <@{user}>, "
                   f"{uid('alex')}, {uid('ana')}, or {uid('francisco')} "
                   "will review your request ASAP!\n" + SUGGESTION)
SUCCESS_FLOW    = ("Hi, <@{user}>, "
                   f"{uid('montse')} or {uid('martin')} will review your "
                   "request ASAP! :alert:\n" + SUGGESTION)
FALLBACK_STATIC = ("Hi, <@{user}>, we couldn't detect your problem in our "
                   "quick-reply catalog. "
                   f"{uid('alex')}, {uid('montse')} or {uid('martin')} "
                   "can guide you further.")

KEYWORDS = {
    # Canal : patrones + respuesta + responsables (IDs Slack)
    "CFT8WFLGY": {                            # #renewals-latam
        "patterns": EN_RENEWALS + ES_RENEWALS,
        "response": SUCCESS_RENEWAL,
        "notify": [USER_IDS["alex"], USER_IDS["ana"], USER_IDS["francisco"]],
    },
    "C01LXQN2D0C": {                          # #renewals-emea
        "patterns": EN_RENEWALS + ES_RENEWALS,
        "response": SUCCESS_RENEWAL,
        "notify": [USER_IDS["alex"], USER_IDS["ana"], USER_IDS["francisco"]],
    },
    "C07261Q282Z": {                          # #renewals-apac
        "patterns": EN_RENEWALS + ES_RENEWALS,
        "response": SUCCESS_RENEWAL,
        "notify": [USER_IDS["alex"], USER_IDS["ana"], USER_IDS["francisco"]],
    },
    "C05KM25RM5W": {                          # #flow-errors
        "patterns": EN_FLOW + ES_FLOW,
        "response": SUCCESS_FLOW,
        "notify": [USER_IDS["montse"], USER_IDS["martin"]],
    },
}

# ── 3 · Google Sheets KB ──────────────────────────────────────
COL_KEYWORDS = "keywords"
COL_STEPS_ES = "steps_es"
COL_STEPS_EN = "steps_en"

def load_kb() -> pd.DataFrame:
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SA_CREDS_PATH, scope)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(GSHEET_ID).worksheet(SHEET_NAME)
    df = pd.DataFrame(ws.get_all_records(numericise_ignore=["all"]))
    logging.info("KB cargada: %s filas", len(df))
    return df

KB = load_kb()

# ── 4 · OCR de imágenes ──────────────────────────────────────
def ocr_from_files(files: list[dict]) -> str:
    text = []
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    for f in files:
        if not f.get("mimetype", "").startswith("image/"):
            continue
        url = f.get("url_private_download")
        if not url:
            continue
        tmp = TEMP_DIR / f"{f['id']}_{f['name']}"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            with open(tmp, "wb") as fp:
                fp.write(r.content)
            text.append(pytesseract.image_to_string(Image.open(tmp)))
        except Exception as e:
            logging.warning("OCR falló: %s", e)
        finally:
            tmp.unlink(missing_ok=True)
    return "\n".join(text)

# ── 5 · Búsqueda en la KB ────────────────────────────────────
def detect_lang(text: str) -> str:
    try:
        return detect(text)
    except LangDetectException:
        return "es"

def kb_answer(text: str) -> Optional[str]:
    lower = text.lower()
    for _, row in KB.iterrows():
        pats = [p.strip().lower() for p in row[COL_KEYWORDS].split(",")]
        if any(re.search(rf"\b{re.escape(p)}\b", lower) for p in pats):
            lang = detect_lang(text)
            return row[COL_STEPS_EN] if lang.startswith("en") and row[COL_STEPS_EN] else row[COL_STEPS_ES]
    # similaridad difusa
    close = difflib.get_close_matches(lower, KB[COL_KEYWORDS].tolist(), n=1, cutoff=0.55)
    if close:
        row = KB[KB[COL_KEYWORDS] == close[0]].iloc[0]
        lang = detect_lang(text)
        return row[COL_STEPS_EN] if lang.startswith("en") and row[COL_STEPS_EN] else row[COL_STEPS_ES]
    return None

# ── 6 · Slack Bolt app ───────────────────────────────────────
app = App(token=SLACK_BOT_TOKEN)
client = WebClient(token=SLACK_BOT_TOKEN)

@app.command("/reloadkb")
def reload_kb_cmd(ack, respond):
    ack()
    global KB
    KB = load_kb()
    respond(f"🔄 KB recargada: {len(KB)} filas")

@app.event("message")
def on_message(event, say):
    if event.get("subtype") in {"bot_message", "message_changed", "message_deleted"}:
        return

    cid   = event["channel"]
    ts    = event["ts"]
    user  = event["user"]
    text  = event.get("text", "")
    files = event.get("files", [])

    full_text = text + "\n" + ocr_from_files(files) if files else text

    # 1) Respuestas fijas
    cfg = KEYWORDS.get(cid)
    if cfg and any(re.search(p, full_text, re.I) for p in cfg["patterns"]):
        say(cfg["response"].format(user=user))
        dm_notify(cfg["notify"], cid, ts)          # ⬅️ DM a responsables
        return

    # 2) KB
    answer = kb_answer(full_text)
    if answer:
        say(f"🛠️ *Pasos sugeridos:*\n{answer}")
        return

    # 3) Fallback
    say(FALLBACK_STATIC.format(user=user))

# ── 7 · Run ──────────────────────────────────────────────────
if __name__ == "__main__":
    logging.info("🚀 FixBuddy arrancando…")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
