# Dependencies: pip install pyTelegramBotAPI opencv-python-headless numpy flask gunicorn

import os
import json
import time
import uuid
import subprocess
import cv2
import numpy as np
from pathlib import Path
from typing import Optional

import telebot
from telebot.types import Message
from flask import Flask, request, abort

# ============================================================
# HARDCODED BOT TOKEN
# ============================================================
BOT_TOKEN = "8500798158:AAFbxISethamvuchRUSlfc7qsZLeZTf_3pU"
# ============================================================

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing hai.")

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


def detect_scenes_opencv(video_path: str) -> list[dict]:
    """OpenCV ka use karke video ke cuts/scenes detect karta hai (Bina AI ke)"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0:
        fps = 30.0
        
    scene_list = []
    prev_frame = None
    frame_count = 0
    threshold = 40.0  # Cut detect karne ka threshold

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Frame ko chhota aur grayscale karo processing ke liye
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 180))

        if prev_frame is not None:
            # Frames ka difference nikalo
            diff = cv2.absdiff(prev_frame, gray)
            mean_diff = np.mean(diff)

            # Agar difference threshold se zyada hai, to scene change hai
            if mean_diff > threshold:
                timestamp = frame_count / fps
                scene_list.append({
                    "scene": len(scene_list) + 1,
                    "start_sec": round(timestamp, 2)
                })

        prev_frame = gray
        frame_count += 1

    cap.release()
    
    # Scene start times ko duration mein convert karo
    for i in range(len(scene_list)):
        if i < len(scene_list) - 1:
            scene_list[i]["end_sec"] = scene_list[i+1]["start_sec"]
        else:
            # Last scene ka end video ki duration hoga
            cap = cv2.VideoCapture(video_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            duration = total_frames / fps if fps > 0 else 0
            cap.release()
            scene_list[i]["end_sec"] = round(duration, 2)
            
        scene_list[i]["duration"] = round(scene_list[i]["end_sec"] - scene_list[i]["start_sec"], 2)

    return scene_list


def apply_edits_to_clip(clip_path: str, output_path: str, ref_scenes: list[dict]) -> bool:
    """Reference video ke scenes ke hisaab se clip ko cut karta hai"""
    clip_info = probe_video(clip_path)
    if "error" in clip_info:
        return False

    clip_duration = float(clip_info.get("format", {}).get("duration", 0))
    if clip_duration <= 0 or not ref_scenes:
        return False

    # Clip ko reference scenes ki length ke hisaab se cut karne ki koshish
    # Hum ek temporary file banayenge jisme sirf cut parts honge
    parts = []
    current_time = 0.0

    for scene in ref_scenes:
        duration_needed = scene["duration"]
        
        # Agar clip mein itna time bacha hai
        if current_time < clip_duration:
            # Cut ki duration clip ke bache hue time se badi nahi honi chahiye
            actual_duration = min(duration_needed, clip_duration - current_time)
            if actual_duration <= 0.1:
                continue
                
            part_file = str(WORK_DIR / f"part_{uuid.uuid4().hex[:6]}.mp4")
            
            # FFmpeg se clip ka ek part kaato
            cmd = [
                "ffmpeg", "-y", "-ss", str(current_time), "-i", clip_path, 
                "-t", str(actual_duration), "-c", "copy", part_file
            ]
            ok, _ = run_cmd(cmd)
            if ok and os.path.exists(part_file):
                parts.append(part_file)
            current_time += actual_duration

    if not parts:
        return False

    # Saare cut parts ko wapas jodo
    concat_file = str(WORK_DIR / "concat.txt")
    with open(concat_file, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")

    concat_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", 
        "-i", concat_file, "-c", "copy", output_path
    ]
    ok, _ = run_cmd(concat_cmd)

    # Cleanup parts
    for p in parts:
        try: os.remove(p)
        except: pass
    try: os.remove(concat_file)
    except: pass

    return ok


@bot.message_handler(commands=["start"])
def cmd_start(m: Message):
    USER_STATE[m.chat.id] = {}
    bot.reply_to(m,
        "Boss man, workflow:\n"
        "1. /reference bhej + reference video\n"
        "2. /clip bhej + apni clip\n"
        "3. /render — OpenCV se cuts match karega\n\n"
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

    bot.reply_to(m, "⏳ Reference video ke scenes detect ho rahe hain (OpenCV)...")
    
    # 1. Reference se scenes nikaalo
    ref_scenes = detect_scenes_opencv(ref)
    if not ref_scenes:
        bot.reply_to(m, "Reference video mein koi clear cuts nahi mile.")
        return

    bot.reply_to(m, f"✅ {len(ref_scenes)} scenes detect hue. Ab teri clip edit ho rahi hai...")

    # 2. Clip ko edit karo
    out = str(WORK_DIR / f"out_{uuid.uuid4().hex[:8]}.mp4")
    ok = apply_edits_to_clip(clip, out, ref_scenes)

    if ok and os.path.exists(out):
        with open(out, "rb") as v:
            bot.send_video(m.chat.id, v, caption="Boss man, OpenCV se edit kiya hua video ready hai.")
    else:
        bot.reply_to(m, "Render fail ho gaya. Check karo ki clip reference se lambi hai ya nahi.")


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
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
