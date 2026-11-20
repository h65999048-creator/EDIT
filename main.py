# Dependencies: pip install pyTelegramBotAPI google-genai flask gunicorn

import os
import json
import time
import uuid
import subprocess
from pathlib import Path
from typing import Optional

import telebot
from telebot.types import Message
from google import genai
from google.genai import types
from flask import Flask, request, abort

# ============================================================
# HARDCODED KEYS (Teri di hui keys)
# ============================================================
BOT_TOKEN = "8500798158:AAFbxISethamvuchRUSlfc7qsZLeZTf_3pU"
GEMINI_API_KEY = "AQ.Ab8RN6L4tOkPcinBwkejIARuOuiMiS2oE8xJR6uPD2fEW4xPEg"
# ============================================================

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise SystemExit("BOT_TOKEN ya GEMINI_API_KEY missing hai.")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = telebot.TeleBot(BOT_TOKEN)

WORK_DIR = Path.home() / "vbot_work"
WORK_DIR.mkdir(exist_ok=True)
USER_STATE: dict[int, dict] = {}

app = Flask(__name__)
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"


def run_cmd(args: list[str], timeout: int = 600) -> tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stderr[-2000:]
    except Exception as e:
        return False, str(e)


def probe_video(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,codec_name",
        "-show_entries", "format=duration,size", "-of", "json", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {"error": proc.stderr[:500]}
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {"error": "parse failed"}


def upload_video_to_gemini(path: str) -> Optional[object]:
    try:
        vf = client.files.upload(file=path)
        while vf.state.name == "PROCESSING":
            time.sleep(3)
            vf = client.files.get(name=vf.name)
        return vf if vf.state.name == "ACTIVE" else None
    except Exception as e:
        print(f"Gemini upload error: {e}")
        return None


def analyze_reference(video_file, user_prompt: str) -> Optional[dict]:
    prompt = f"""
You are a professional video editor. Analyze this reference video CAREFULLY.
User's intent: {user_prompt}

Return STRICT JSON only, no markdown:
{{
  "total_duration_sec": <float>,
  "scene_count": <int>,
  "scenes": [
    {{"start_sec": <float>, "end_sec": <float>, "description": "<what happens>",
      "camera_movement": "<static/pan/zoom/handheld>", "color_tone": "<warm/cool/neutral/high-contrast>",
      "transition_to_next": "<cut/fade/dissolve/none>"}}
  ],
  "music_mood": "<energetic/calm/dramatic/happy/none>",
  "editing_pace": "<fast/medium/slow>",
  "text_overlays": <true/false>,
  "overall_style": "<brief summary>"
}}
"""
    try:
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[video_file, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        text = resp.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"Analysis error: {e}")
        return None


def generate_edit_plan(analysis: dict, clip_info: dict) -> Optional[dict]:
    prompt = f"""
Reference analysis: {json.dumps(analysis)}
My clip info: {json.dumps(clip_info)}

Return STRICT JSON edit plan, no markdown:
{{
  "operations": [
    {{"type": "trim", "start_sec": <f>, "end_sec": <f>, "reason": "<why>"}},
    {{"type": "setpts", "speed_factor": <0.5-2.0>, "reason": "<why>"}},
    {{"type": "eq", "brightness": <-1..1>, "contrast": <0..2>, "saturation": <0..2>, "reason": "<why>"}}
  ],
  "output_notes": "<what final should look like>"
}}
Rules: Only types trim/setpts/eq. Max 5 ops. Be realistic with clip duration.
"""
    try:
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        text = resp.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"Plan error: {e}")
        return None


def apply_edit_plan(input_path: str, output_path: str, plan: dict) -> bool:
    vfilters, afilters = [], []
    trim = None
    speed = 1.0
    for op in plan.get("operations", []):
        t = op.get("type")
        if t == "trim":
            trim = (float(op.get("start_sec", 0)), float(op.get("end_sec", 0)))
        elif t == "setpts":
            speed = max(0.5, min(2.0, float(op.get("speed_factor", 1.0))))
        elif t == "eq":
            vfilters.append(
                f"eq=brightness={op.get('brightness',0)}:"
                f"contrast={op.get('contrast',1)}:"
                f"saturation={op.get('saturation',1)}"
            )

    if speed != 1.0:
        vfilters.insert(0, f"setpts={1/speed}*PTS")
        afilters.append(f"atempo={speed}")

    if trim:
        dur = trim[1] - trim[0]
        if dur <= 0:
            return False
        cmd = ["ffmpeg", "-y", "-ss", str(trim[0]), "-i", input_path, "-t", str(dur)]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_path]

    if vfilters:
        cmd += ["-vf", ",".join(vfilters)]
    if afilters:
        cmd += ["-af", ",".join(afilters)]

    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", output_path]

    ok, err = run_cmd(cmd)
    if not ok:
        print(f"ffmpeg error: {err}")
    return ok


@bot.message_handler(commands=["start"])
def cmd_start(m: Message):
    USER_STATE[m.chat.id] = {}
    bot.reply_to(m,
        "Boss man, workflow:\n"
        "1. /reference bhej + reference video\n"
        "2. /clip bhej + apni clip\n"
        "3. /render — Gemini analyze + ffmpeg edit\n\n"
        "Commands: /reference /clip /render /status")


@bot.message_handler(commands=["reference"])
def cmd_ref(m: Message):
    USER_STATE.setdefault(m.chat.id, {})["stage"] = "awaiting_reference"
    bot.reply_to(m, "Reference video bhej.")


@bot.message_handler(commands=["clip"])
def cmd_clip(m: Message):
    USER_STATE.setdefault(m.chat.id, {})["stage"] = "awaiting_clip"
    bot.reply_to(m, "Apni clip bhej.")


@bot.message_handler(commands=["status"])
def cmd_status(m: Message):
    s = USER_STATE.get(m.chat.id, {})
    bot.reply_to(m,
        f"ref={'✅' if s.get('reference_path') else '❌'} "
        f"clip={'✅' if s.get('clip_path') else '❌'} "
        f"stage={s.get('stage','none')}")


@bot.message_handler(content_types=["video", "document"])
def handle_file(m: Message):
    s = USER_STATE.get(m.chat.id, {})
    stage = s.get("stage")
    if not stage:
        bot.reply_to(m, "Pehle /reference ya /clip use kar.")
        return

    file_id = m.video.file_id if m.video else (m.document.file_id if m.document else None)
    name = (m.document.file_name if m.document and m.document.file_name
            else f"{uuid.uuid4().hex}.mp4")
    if not file_id:
        return

    try:
        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)
        p = WORK_DIR / name
        p.write_bytes(data)
    except Exception as e:
        bot.reply_to(m, f"Download fail: {e}")
        return

    if stage == "awaiting_reference":
        s["reference_path"] = str(p)
        s["stage"] = "reference_ready"
        bot.reply_to(m, f"Reference saved: {name}\nAb /clip bhej.")
    elif stage == "awaiting_clip":
        s["clip_path"] = str(p)
        s["stage"] = "clip_ready"
        bot.reply_to(m, f"Clip saved: {name}\nAb /render bhej.")


@bot.message_handler(commands=["render"])
def cmd_render(m: Message):
    s = USER_STATE.get(m.chat.id, {})
    ref, clip = s.get("reference_path"), s.get("clip_path")
    if not ref or not clip or not (os.path.exists(ref) and os.path.exists(clip)):
        bot.reply_to(m, "Reference + clip dono chahiye.")
        return

    bot.reply_to(m, "⏳ Gemini analyze kar raha hai...")
    vf = upload_video_to_gemini(ref)
    if not vf:
        bot.reply_to(m, "Gemini upload fail.")
        return

    analysis = analyze_reference(vf, "Match style, cuts, color tone, pace.")
    if not analysis:
        bot.reply_to(m, "Analysis fail.")
        return

    bot.reply_to(m, f"✅ {analysis.get('scene_count','?')} scenes | style: {analysis.get('overall_style','?')}")

    clip_info = probe_video(clip)
    if "error" in clip_info:
        bot.reply_to(m, f"Clip probe fail: {clip_info['error']}")
        return

    plan = generate_edit_plan(analysis, clip_info)
    if not plan:
        bot.reply_to(m, "Edit plan fail.")
        return

    out = str(WORK_DIR / f"out_{uuid.uuid4().hex[:8]}.mp4")
    ok = apply_edit_plan(clip, out, plan)

    if ok and os.path.exists(out):
        with open(out, "rb") as v:
            bot.send_video(m.chat.id, v, caption=f"Style: {analysis.get('overall_style','N/A')}")
    else:
        bot.reply_to(m, "Render fail.")


@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "", 200


@app.route("/health")
def health():
    return "OK", 200


@app.route("/")
def index():
    return "Bot is running", 200


if __name__ == "__main__":
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=webhook_url)
        print(f"Webhook set: {webhook_url}")
    else:
        print("RENDER_EXTERNAL_URL nahi mila, webhook set nahi hua.")

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
