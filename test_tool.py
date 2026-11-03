import os
import re
import time
import datetime
import queue
import requests
import threading
import pandas as pd
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import messagebox, ttk
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

SESSION_FILE = "fano_session.txt"
task_queue = []
log_queue = queue.Queue()

pause_event = threading.Event()
pause_event.set()
cancel_flag = False

def load_saved_token():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""

def save_token(token):
    try:
        with open(SESSION_FILE, "w") as f:
            f.write(token.strip())
    except Exception:
        pass

def format_timestamp(seconds):
    if seconds is None:
        return "00:00:00.000"
    try:
        td = timedelta(seconds=float(seconds))
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        millis = int(td.microseconds / 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    except Exception:
        return str(seconds)

def log_message(text):
    log_queue.put(str(text))

def flush_log_messages():
    messages = []
    while True:
        try:
            messages.append(log_queue.get_nowait())
        except queue.Empty:
            break

    if messages:
        log_box.insert(tk.END, "\n".join(messages) + "\n")
        log_box.see(tk.END)

    root.after(100, flush_log_messages)

def get_auth_header(token):
    clean_token = token.strip()
    if not clean_token.lower().startswith("bearer "):
        return "Bearer " + clean_token
    return clean_token

def extract_user_name(val):
    if not val:
        return "N/A"
    if isinstance(val, dict):
        first = str(val.get("first_name", "")).strip()
        last = str(val.get("last_name", "")).strip()
        if last and first:
            return f"{last} {first}"
        return first or last or val.get("username") or val.get("name") or val.get("id") or "N/A"
    return str(val)

def fetch_task_details(task_id, auth_header, custom_project_name):
    endpoints = [
        f"https://tms-ext.fano.ai/api/auto/admin/tasks/{task_id}",
        f"https://tms-ext.fano.ai/api/auto/tasks/{task_id}"
    ]
    
    headers = {
        "Authorization": auth_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    file_name = task_id
    worker = "N/A"
    checker = "N/A"
    project_name = custom_project_name if custom_project_name else "Unknown Project"

    for url in endpoints:
        try:
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict):
                    task_data = data.get("data", data)
                    
                    found_name = (
                        task_data.get("file_name") or 
                        task_data.get("filename") or 
                        task_data.get("name") or 
                        task_data.get("audio_name") or 
                        task_data.get("title") or 
                        task_data.get("task_name")
                    )
                    if found_name:
                        file_name = found_name
                    
                    raw_worker = (
                        task_data.get("worker") or 
                        task_data.get("assignee") or 
                        task_data.get("worker_name") or 
                        task_data.get("assignee_name") or
                        task_data.get("user")
                    )
                    if raw_worker:
                        worker = extract_user_name(raw_worker)
                    
                    raw_checker = (
                        task_data.get("checker") or 
                        task_data.get("checker_name") or
                        task_data.get("auditor")
                    )
                    if raw_checker:
                        checker = extract_user_name(raw_checker)

                    found_proj = (
                        task_data.get("project_name") or 
                        task_data.get("project")
                    )
                    if found_proj:
                        if isinstance(found_proj, dict):
                            project_name = found_proj.get("name") or found_proj.get("title") or custom_project_name
                        else:
                            project_name = str(found_proj)
                    break
        except Exception:
            pass

    return {
        "file_name": file_name,
        "worker": worker,
        "checker": checker,
        "project_name": project_name
    }

def fetch_task_segments(url, token, custom_project_name):
    parsed_url = urlsplit(url)
    task_id = parsed_url.path.rstrip("/").rsplit("/", 1)[-1].strip()
    if not parsed_url.scheme or not parsed_url.netloc or not task_id or "tasks" not in parsed_url.path.split("/"):
        return None, "Invalid Task URL format"

    auth_header = get_auth_header(token)
    meta = fetch_task_details(task_id, auth_header, custom_project_name)

    transcripts_api_url = (
        f"{parsed_url.scheme}://{parsed_url.netloc}/api/auto/transcripts?"
        f"{urlencode({'task_id': task_id})}"
    )
    headers = {
        "Authorization": auth_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        res = requests.get(transcripts_api_url, headers=headers, timeout=12)
        if res.status_code == 401:
            return None, "HTTP 401 Unauthorized"
        if res.status_code != 200:
            return None, f"HTTP Error {res.status_code}"

        res_data = res.json()
        if isinstance(res_data, list):
            segments = res_data
        elif isinstance(res_data, dict):
            segments = (
                res_data.get("segments") or 
                res_data.get("utterances") or 
                res_data.get("data", [])
            )
        else:
            segments = []
        
        if not segments:
            return [], None

        extracted_rows = []
        for idx, seg in enumerate(segments, start=1):
            seg_num = seg.get("index", seg.get("order", idx))
            
            raw_status = (
                seg.get("status") or 
                seg.get("check_status") or 
                seg.get("checkStatus") or 
                seg.get("review_status") or 
                seg.get("segment_status") or 
                seg.get("state") or 
                seg.get("result")
            )
            
            if raw_status is not None:
                if isinstance(raw_status, dict):
                    status = str(raw_status.get("label", raw_status.get("name", "Pass")))
                else:
                    status = str(raw_status).capitalize()
            else:
                status = "Pass"
            
            raw_role = seg.get("speaker_role", seg.get("role", seg.get("speaker", "")))
            if isinstance(raw_role, dict):
                role = raw_role.get("role", raw_role.get("speaker", str(raw_role)))
            else:
                role = str(raw_role)

            gender = seg.get("gender") or seg.get("speaker_gender") or seg.get("speakerGender") or ""
            if not gender and isinstance(raw_role, dict):
                gender = raw_role.get("gender", raw_role.get("speaker_gender", ""))
            
            if not gender:
                speaker_info = seg.get("speaker_info", seg.get("speaker_meta", {}))
                if isinstance(speaker_info, dict):
                    gender = speaker_info.get("gender", speaker_info.get("speaker_gender", ""))

            gender = str(gender).capitalize() if gender else "Female"

            language = seg.get("language", "Spanish")
            text = seg.get("text", seg.get("transcription", seg.get("content", "")))
            
            start_sec = seg.get("start_time", seg.get("start", 0))
            end_sec = seg.get("end_time", seg.get("end", 0))
            
            try:
                duration = round(float(end_sec) - float(start_sec), 2)
            except Exception:
                duration = 0

            extracted_rows.append({
                "Project Name": meta["project_name"],
                "File Name": meta["file_name"],
                "Worker": meta["worker"],
                "Checker": meta["checker"],
                "Segment #": seg_num,
                "Status": status,
                "Role": role.capitalize() if role else "Agent",
                "Gender": gender,
                "Language": language,
                "Transcription Text": text,
                "Start Time": format_timestamp(start_sec),
                "End Time": format_timestamp(end_sec),
                "Duration (sec)": duration
            })

        return extracted_rows, None

    except Exception as e:
        return None, f"Network Exception: {str(e)}"

def download_audio_file(task_url, auth_token):
    from io import BytesIO
    from urllib.parse import urlparse
    from zipfile import BadZipFile, ZipFile

    parsed = urlparse(task_url)
    path_parts = parsed.path.strip("/").split("/")
    task_id = path_parts[-1] if path_parts else ""

    if not task_id:
        print("[ERROR] Could not extract task_id from URL.")
        return None

    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "https://tms-ext.fano.ai"
    audio_api_url = f"{base_url}/api/auto/tasks/{task_id}/audio"
    headers = {
        "Authorization": get_auth_header(auth_token),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    output_dir = r"C:\Users\ashis\Downloads\Music"
    os.makedirs(output_dir, exist_ok=True)

    try:
        log_message(f"Downloading audio for task {task_id}...")
        with requests.get(audio_api_url, headers=headers, stream=True, timeout=30) as res:
            content_type = res.headers.get("Content-Type", "").lower()

            if res.status_code != 200:
                log_message(f"[ERROR] Audio request failed: HTTP {res.status_code}; Content-Type: {content_type or 'missing'}")
                log_message(f"[SERVER RESPONSE] {res.text}")
                return None

            if "json" in content_type or "html" in content_type:
                log_message(f"[ERROR] Server returned non-audio content ({content_type}):")
                log_message(res.text)
                return None

            filename = f"Task_Audio_{task_id}.wav"
            file_path = os.path.join(output_dir, filename)

            if "zip" in content_type:
                try:
                    with ZipFile(BytesIO(res.content)) as archive:
                        wav_files = [
                            name for name in archive.namelist()
                            if name.lower().endswith(".wav") and not name.endswith("/")
                        ]
                        if not wav_files:
                            log_message("[ERROR] ZIP response contains no .wav file.")
                            log_message(f"[ZIP CONTENTS] {', '.join(archive.namelist())}")
                            return None

                        with archive.open(wav_files[0]) as audio_data, open(file_path, "wb") as f:
                            while True:
                                chunk = audio_data.read(8192)
                                if not chunk:
                                    break
                                f.write(chunk)
                except BadZipFile:
                    log_message("[ERROR] Server reported a ZIP response, but the response is not a valid ZIP archive.")
                    return None
                return file_path

            if "audio" not in content_type and "octet-stream" not in content_type:
                log_message(f"[ERROR] Refusing to save response with Content-Type: {content_type or 'missing'}")
                return None

            with open(file_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            return file_path

    except Exception as e:
        log_message(f"[ERROR] Exception during audio download: {e}")
        return None

def audio_download_thread(url, token):
    file_path = download_audio_file(url, get_auth_header(token))
    if file_path:
        log_message(f"[SUCCESS] Audio saved to: {file_path}")
        status_label.config(text="Audio Download Complete!", fg="#4CAF50")
    else:
        status_label.config(text="Audio Download Failed", fg="#E53935")

def trigger_audio_download():
    raw_input = url_box.get("1.0", tk.END).strip()
    token = token_entry.get().strip()

    urls = re.findall(r'https?://[^\s,\n"]+', raw_input)
    if not urls or not token:
        messagebox.showerror("Input Error", "Please provide a Task URL and Authorization Token.")
        return

    save_token(token)
    threading.Thread(target=audio_download_thread, args=(urls[0], token), daemon=True).start()

def single_excel_thread(url, token, current_project):
    rows, err = fetch_task_segments(url, token, current_project)
    if err:
        log_message(f"[ERROR] {err}")
        status_label.config(text="Single Export Failed", fg="#E53935")
        return

    if not rows:
        log_message("[WARNING] No segments found.")
        status_label.config(text="No Segments Found", fg="#FF9800")
        return

    df = pd.DataFrame(rows)
    music_dir = os.path.join(os.path.expanduser("~"), "Downloads", "Music")
    if not os.path.exists(music_dir):
        os.makedirs(music_dir)

    task_id = url.split("/tasks/")[-1].split("?")[0].strip()
    excel_filepath = os.path.join(music_dir, f"Task_Transcription_{task_id[:8]}.xlsx")
    
    try:
        df.to_excel(excel_filepath, index=False, engine="openpyxl")
    except Exception:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        excel_filepath = os.path.join(music_dir, f"Task_Transcription_{task_id[:8]}_{ts}.xlsx")
        df.to_excel(excel_filepath, index=False, engine="openpyxl")

    log_message(f"[SUCCESS] Exported {len(rows)} rows to:\n{excel_filepath}")
    status_label.config(text="Single Excel Complete!", fg="#4CAF50")

def trigger_single_excel():
    raw_input = url_box.get("1.0", tk.END).strip()
    token = token_entry.get().strip()
    current_project = project_entry.get().strip()

    urls = re.findall(r'https?://[^\s,\n"]+', raw_input)
    if not urls or not token:
        messagebox.showerror("Input Error", "Please provide a Task URL and Authorization Token.")
        return

    save_token(token)
    log_message(f"Fetching single task transcription for {urls[0]}...")
    threading.Thread(target=single_excel_thread, args=(urls[0], token, current_project), daemon=True).start()

def add_multiple_to_queue():
    raw_input = url_box.get("1.0", tk.END).strip()
    token = token_entry.get().strip()

    if not raw_input or not token:
        messagebox.showerror("Input Error", "Please provide Task URLs and Authorization Token.")
        return

    save_token(token)
    urls = re.findall(r'https?://[^\s,\n"]+', raw_input)
    
    if not urls:
        messagebox.showwarning("No Links Found", "No valid HTTP/HTTPS links detected.")
        return

    added_count = 0
    for u in urls:
        task_queue.append((u.strip(), token))
        added_count += 1

    url_box.delete("1.0", tk.END)
    queue_count_label.config(text=f"{len(task_queue)} TASKS IN QUEUE")
    log_message(f"Added {added_count} task link(s) to batch queue.")

def toggle_pause():
    if pause_event.is_set():
        pause_event.clear()
        pause_btn.config(text="▶ Resume Batch", bg="#27AE60")
        status_label.config(text="Batch Paused", fg="#F9E2AF")
        log_message("[PAUSED] Batch processing suspended.")
    else:
        pause_event.set()
        pause_btn.config(text="⏸ Pause", bg="#F39C12")
        status_label.config(text="Processing Batch...", fg="#89B4FA")
        log_message("[RESUMED] Continuing batch processing.")

def cancel_batch():
    global cancel_flag, task_queue
    cancel_flag = True
    pause_event.set()
    task_queue.clear()
    queue_count_label.config(text="0 TASKS IN QUEUE")
    log_message("[CANCELLED] Batch aborted. Queue has been reset.")
    status_label.config(text="Batch Cancelled / Reset", fg="#E53935")
    master_btn.config(state=tk.NORMAL)
    pause_btn.config(state=tk.DISABLED, text="⏸ Pause", bg="#F39C12")
    cancel_btn.config(state=tk.DISABLED)

def process_batch_task(idx, total_tasks, url, token, current_project, operation_mode):
    pause_event.wait()
    if cancel_flag:
        return idx, None, None, "Cancelled"

    log_message(f"Processing Task {idx}/{total_tasks}...")
    rows = None
    err = None
    audio_path = None

    if operation_mode in ("Excel Only", "Both Excel and Audio"):
        rows, err = fetch_task_segments(url, token, current_project)

    if operation_mode in ("Audio Only", "Both Excel and Audio"):
        audio_path = download_audio_file(url, token)
        if audio_path:
            log_message(f"[SUCCESS Task {idx}] Audio saved to: {audio_path}")
        else:
            log_message(f"[ERROR Task {idx}] Audio download failed.")

    return idx, rows, audio_path, err

def finish_batch_ui(status_text, status_color, batch_tasks=None):
    if batch_tasks is not None:
        for task in batch_tasks:
            try:
                task_queue.remove(task)
            except ValueError:
                pass
        queue_count_label.config(text=f"{len(task_queue)} TASKS IN QUEUE")

    status_label.config(text=status_text, fg=status_color)
    master_btn.config(state=tk.NORMAL)
    pause_btn.config(state=tk.DISABLED)
    cancel_btn.config(state=tk.DISABLED)

def process_batch_thread(batch_tasks, current_token, current_project, operation_mode):
    global cancel_flag
    log_message(f"--- STARTING BATCH PROCESSING ({len(batch_tasks)} Tasks) ---")
    master_rows = []
    successful_audio_downloads = 0
    blank_row = {
        "Project Name": "", "File Name": "", "Worker": "", "Checker": "",
        "Segment #": "", "Status": "", "Role": "", "Gender": "",
        "Language": "", "Transcription Text": "", "Start Time": "",
        "End Time": "", "Duration (sec)": ""
    }

    total_tasks = len(batch_tasks)
    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(
                process_batch_task, idx, total_tasks, url, current_token,
                current_project, operation_mode
            )
            for idx, (url, _) in enumerate(batch_tasks, start=1)
        ]
        for future in as_completed(futures):
            idx, rows, audio_path, err = future.result()
            results[idx] = (rows, audio_path, err)

    if cancel_flag:
        root.after(0, finish_batch_ui, "Batch Cancelled / Reset", "#E53935")
        return

    for idx in range(1, total_tasks + 1):
        rows, audio_path, err = results[idx]
        if operation_mode in ("Excel Only", "Both Excel and Audio") and err:
            log_message(f"[ERROR Task {idx}] {err}")
        if operation_mode in ("Audio Only", "Both Excel and Audio") and audio_path:
            successful_audio_downloads += 1
        if rows:
            master_rows.extend(rows)
            if idx < total_tasks:
                master_rows.append(blank_row)

    excel_enabled = operation_mode in ("Excel Only", "Both Excel and Audio")
    audio_enabled = operation_mode in ("Audio Only", "Both Excel and Audio")
    if excel_enabled and not master_rows:
        log_message("[ERROR] All queued tasks failed or returned empty data.")
        if not audio_enabled or successful_audio_downloads == 0:
            root.after(0, finish_batch_ui, "Batch Processing Failed", "#E53935")
            return
        root.after(
            0,
            finish_batch_ui,
            "Audio Complete; Excel Export Failed",
            "#FF9800",
            batch_tasks
        )
        return

    if not excel_enabled:
        if successful_audio_downloads == 0:
            log_message("[ERROR] All queued audio downloads failed.")
            root.after(0, finish_batch_ui, "Batch Audio Downloads Failed", "#E53935")
            return
        log_message(f"[SUCCESS] Downloaded audio for {successful_audio_downloads} of {total_tasks} task(s).")
        root.after(0, finish_batch_ui, "Batch Audio Downloads Complete!", "#4CAF50", batch_tasks)
        return

    try:
        log_message("Compiling and saving Master Excel sheet...")
        df = pd.DataFrame(master_rows)
        music_dir = os.path.join(os.path.expanduser("~"), "Downloads", "Music")
        if not os.path.exists(music_dir):
            os.makedirs(music_dir)

        master_filepath = os.path.join(music_dir, "Master_Batch_Transcriptions.xlsx")
        
        try:
            df.to_excel(master_filepath, index=False, engine="openpyxl")
        except Exception:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            master_filepath = os.path.join(music_dir, f"Master_Batch_Transcriptions_{ts}.xlsx")
            df.to_excel(master_filepath, index=False, engine="openpyxl")

        log_message(f"[SUCCESS] Master Excel Created with {len(master_rows)} total rows!")
        log_message(f"Saved to: {master_filepath}")
        if audio_enabled and successful_audio_downloads < total_tasks:
            finish_status = "Excel Created; Some Audio Downloads Failed"
            finish_color = "#FF9800"
        elif audio_enabled:
            finish_status = "Batch Excel and Audio Complete!"
            finish_color = "#4CAF50"
        else:
            finish_status = "Master Batch Excel Created!"
            finish_color = "#4CAF50"
    except Exception as e:
        log_message(f"[ERROR Saving Excel] {str(e)}")
        if audio_enabled and successful_audio_downloads:
            finish_status = "Audio Complete; Failed to Save Excel"
            finish_color = "#FF9800"
        else:
            finish_status = "Failed to Save Excel File"
            finish_color = "#E53935"

    if not audio_enabled or successful_audio_downloads:
        root.after(0, finish_batch_ui, finish_status, finish_color, batch_tasks)
    else:
        root.after(0, finish_batch_ui, finish_status, finish_color)

def export_master_excel():
    global cancel_flag
    if not task_queue:
        messagebox.showwarning("Queue Empty", "Please add at least one task link to the queue first.")
        return

    batch_tasks = list(task_queue)
    current_token = token_entry.get().strip()
    current_project = project_entry.get().strip()
    operation_mode = operation_mode_var.get()
    cancel_flag = False
    master_btn.config(state=tk.DISABLED)
    pause_btn.config(state=tk.NORMAL, text="⏸ Pause", bg="#F39C12")
    cancel_btn.config(state=tk.NORMAL)
    status_label.config(text="Processing Batch in Background...", fg="#89B4FA")
    threading.Thread(
        target=process_batch_thread,
        args=(batch_tasks, current_token, current_project, operation_mode),
        daemon=True
    ).start()

root = tk.Tk()
root.title("Fano Workflow Automation Suite")
root.geometry("760x900")
root.minsize(700, 820)

COLORS = {
    "background": "#0B1020",
    "surface": "#121A2B",
    "surface_alt": "#182338",
    "border": "#25334D",
    "text": "#E7ECF5",
    "muted": "#91A0B8",
    "accent": "#7C5CFC",
    "accent_hover": "#927AFF",
    "cyan": "#46C8D6",
    "green": "#23B889",
    "orange": "#E8A64A",
    "red": "#E45B70",
    "input": "#0D1526",
    "log": "#09111F",
}
root.configure(bg=COLORS["background"])
root.grid_columnconfigure(0, weight=1)
root.grid_rowconfigure(4, weight=1)

style = ttk.Style(root)
style.theme_use("clam")
style.configure(
    "Modern.TCombobox",
    fieldbackground=COLORS["input"],
    background=COLORS["surface_alt"],
    foreground=COLORS["text"],
    arrowcolor=COLORS["cyan"],
    bordercolor=COLORS["border"],
    lightcolor=COLORS["border"],
    darkcolor=COLORS["border"],
    padding=(10, 7),
    font=("Segoe UI", 10),
)
style.configure(
    "Vertical.TScrollbar",
    background=COLORS["surface_alt"],
    troughcolor=COLORS["input"],
    arrowcolor=COLORS["muted"],
    bordercolor=COLORS["surface"],
)
style.map(
    "Modern.TCombobox",
    fieldbackground=[("readonly", COLORS["input"])],
    foreground=[("readonly", COLORS["text"])],
    selectbackground=[("readonly", COLORS["input"])],
    selectforeground=[("readonly", COLORS["text"])],
)

def make_card(parent, **kwargs):
    return tk.Frame(
        parent,
        bg=COLORS["surface"],
        highlightbackground=COLORS["border"],
        highlightthickness=1,
        **kwargs
    )

def make_button(parent, text, command, color, hover_color, **kwargs):
    options = {
        "text": text,
        "command": command,
        "bg": color,
        "fg": "#FFFFFF",
        "activebackground": hover_color,
        "activeforeground": "#FFFFFF",
        "disabledforeground": "#FFFFFF",
        "font": ("Segoe UI", 9, "bold"),
        "relief": tk.FLAT,
        "borderwidth": 0,
        "padx": 13,
        "pady": 9,
        "cursor": "hand2",
    }
    options.update(kwargs)
    button = tk.Button(parent, **options)
    button.bind("<Enter>", lambda _event: button.config(bg=hover_color) if str(button["state"]) != tk.DISABLED else None)
    button.bind("<Leave>", lambda _event: button.config(bg=color))
    return button

header = tk.Frame(root, bg=COLORS["background"])
header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 18))
header.grid_columnconfigure(1, weight=1)
tk.Label(
    header,
    text="FANO WORKFLOW",
    bg=COLORS["background"],
    fg=COLORS["text"],
    font=("Segoe UI", 19, "bold"),
).grid(row=0, column=0, sticky="sw")
tk.Label(
    header,
    text="AUDIO & TRANSCRIPTION STUDIO",
    bg=COLORS["background"],
    fg=COLORS["muted"],
    font=("Segoe UI", 9, "bold"),
).grid(row=1, column=0, sticky="nw", pady=(3, 0))
tk.Label(
    header,
    text="DD.DEV",
    bg=COLORS["background"],
    fg="#8170FF",
    font=("Segoe UI", 19, "bold italic"),
).grid(row=0, column=2, sticky="se")
tk.Label(
    header,
    text="A PRODUCT OF DD.DEV",
    bg=COLORS["background"],
    fg=COLORS["muted"],
    font=("Segoe UI", 8, "bold"),
).grid(row=1, column=2, sticky="ne", pady=(3, 0))

input_card = make_card(root)
input_card.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 12))
input_card.grid_columnconfigure(0, weight=1)
tk.Label(
    input_card,
    text="01   TASK INPUT",
    bg=COLORS["surface"],
    fg=COLORS["cyan"],
    font=("Segoe UI", 9, "bold"),
).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 10))
tk.Label(
    input_card,
    text="Paste one or more task links",
    bg=COLORS["surface"],
    fg=COLORS["muted"],
    font=("Segoe UI", 9),
).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 5))
url_frame = tk.Frame(input_card, bg=COLORS["input"], highlightbackground=COLORS["border"], highlightthickness=1)
url_frame.grid(row=2, column=0, sticky="ew", padx=18)
url_frame.grid_columnconfigure(0, weight=1)
url_box = tk.Text(
    url_frame,
    height=4,
    bg=COLORS["input"],
    fg=COLORS["text"],
    insertbackground=COLORS["cyan"],
    selectbackground=COLORS["accent"],
    relief=tk.FLAT,
    borderwidth=0,
    padx=11,
    pady=9,
    font=("Consolas", 10),
    wrap=tk.WORD,
)
url_box.grid(row=0, column=0, sticky="nsew")

credentials = tk.Frame(input_card, bg=COLORS["surface"])
credentials.grid(row=3, column=0, sticky="ew", padx=18, pady=(13, 17))
credentials.grid_columnconfigure(0, weight=1)
credentials.grid_columnconfigure(1, weight=1)
tk.Label(
    credentials,
    text="AUTHORIZATION TOKEN",
    bg=COLORS["surface"],
    fg=COLORS["muted"],
    font=("Segoe UI", 8, "bold"),
).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 5))
tk.Label(
    credentials,
    text="PROJECT / FOLDER NAME",
    bg=COLORS["surface"],
    fg=COLORS["muted"],
    font=("Segoe UI", 8, "bold"),
).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 5))
token_entry = tk.Entry(
    credentials,
    bg=COLORS["input"],
    fg="#A7E8C9",
    insertbackground=COLORS["cyan"],
    relief=tk.FLAT,
    borderwidth=0,
    highlightthickness=1,
    highlightbackground=COLORS["border"],
    highlightcolor=COLORS["accent"],
    font=("Segoe UI", 10),
)
token_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10), ipady=9, ipadx=10)

saved_token = load_saved_token()
if saved_token:
    token_entry.insert(0, saved_token)

project_entry = tk.Entry(
    credentials,
    bg=COLORS["input"],
    fg=COLORS["text"],
    insertbackground=COLORS["cyan"],
    relief=tk.FLAT,
    borderwidth=0,
    highlightthickness=1,
    highlightbackground=COLORS["border"],
    highlightcolor=COLORS["accent"],
    font=("Segoe UI", 10),
)
project_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0), ipady=9, ipadx=10)
project_entry.insert(0, "2026-31week-DCP-1266-06-MexicanAccent-ConversationSeparated")

actions_card = make_card(root)
actions_card.grid(row=2, column=0, sticky="ew", padx=28, pady=(0, 12))
actions_card.grid_columnconfigure(0, weight=1)
tk.Label(
    actions_card,
    text="02   CHOOSE AN ACTION",
    bg=COLORS["surface"],
    fg=COLORS["cyan"],
    font=("Segoe UI", 9, "bold"),
).grid(row=0, column=0, sticky="w", padx=18, pady=(15, 11))

operation_mode_var = tk.StringVar(value="Excel Only")
operation_mode_frame = tk.Frame(actions_card, bg=COLORS["surface"])
operation_mode_frame.grid(row=1, column=0, sticky="ew", padx=18)
operation_mode_frame.grid_columnconfigure(1, weight=1)
tk.Label(
    operation_mode_frame,
    text="Operation Mode",
    font=("Segoe UI", 9, "bold"),
    fg=COLORS["text"],
    bg=COLORS["surface"],
).grid(row=0, column=0, sticky="w", padx=(0, 14))
operation_mode_dropdown = ttk.Combobox(
    operation_mode_frame,
    textvariable=operation_mode_var,
    values=("Excel Only", "Audio Only", "Both Excel and Audio"),
    state="readonly",
    width=24,
    style="Modern.TCombobox",
)
operation_mode_dropdown.grid(row=0, column=1, sticky="ew")

btn_frame1 = tk.Frame(actions_card, bg=COLORS["surface"])
btn_frame1.grid(row=2, column=0, sticky="ew", padx=18, pady=(13, 16))
for column in range(3):
    btn_frame1.grid_columnconfigure(column, weight=1)

audio_btn = make_button(btn_frame1, "Download Audio", trigger_audio_download, "#167D68", "#1C9B7F")
audio_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

single_btn = make_button(btn_frame1, "Export Single Excel", trigger_single_excel, "#2857A5", "#326CCB")
single_btn.grid(row=0, column=1, sticky="ew", padx=6)

add_queue_btn = make_button(btn_frame1, "＋  Add to Queue", add_multiple_to_queue, "#7A4DD8", "#9167ED")
add_queue_btn.grid(row=0, column=2, sticky="ew", padx=(6, 0))

batch_card = make_card(root)
batch_card.grid(row=3, column=0, sticky="ew", padx=28, pady=(0, 12))
batch_card.grid_columnconfigure(0, weight=1)
batch_header = tk.Frame(batch_card, bg=COLORS["surface"])
batch_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(15, 10))
batch_header.grid_columnconfigure(0, weight=1)
tk.Label(
    batch_header,
    text="03   BATCH CONTROL",
    bg=COLORS["surface"],
    fg=COLORS["cyan"],
    font=("Segoe UI", 9, "bold"),
).grid(row=0, column=0, sticky="w")
queue_count_label = tk.Label(
    batch_header,
    text="0 TASKS IN QUEUE",
    font=("Segoe UI", 8, "bold"),
    fg=COLORS["text"],
    bg=COLORS["surface_alt"],
    padx=10,
    pady=6,
)
queue_count_label.grid(row=0, column=1, sticky="e")

btn_frame2 = tk.Frame(batch_card, bg=COLORS["surface"])
btn_frame2.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 11))
btn_frame2.grid_columnconfigure(0, weight=3)
btn_frame2.grid_columnconfigure(1, weight=1)
btn_frame2.grid_columnconfigure(2, weight=1)

master_btn = make_button(btn_frame2, "▶   RUN BATCH QUEUE", export_master_excel, COLORS["accent"], COLORS["accent_hover"], font=("Segoe UI", 10, "bold"), pady=11)
master_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

pause_btn = make_button(
    btn_frame2,
    "Ⅱ  Pause",
    toggle_pause,
    "#A96C23",
    "#C4822D",
    state=tk.DISABLED,
    disabledforeground="#FFF8EA",
)
pause_btn.grid(row=0, column=1, sticky="ew", padx=6)

cancel_btn = make_button(
    btn_frame2,
    "✕  Cancel",
    cancel_batch,
    "#A94154",
    "#C75167",
    state=tk.DISABLED,
    disabledforeground="#FFF1F3",
)
cancel_btn.grid(row=0, column=2, sticky="ew", padx=(6, 0))

status_label = tk.Label(
    batch_card,
    text="●  Ready for your next batch",
    font=("Segoe UI", 9),
    fg=COLORS["muted"],
    bg=COLORS["surface"],
    anchor="w",
)
status_label.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 15))

log_card = make_card(root)
log_card.grid(row=4, column=0, sticky="nsew", padx=28, pady=(0, 22))
log_card.grid_columnconfigure(0, weight=1)
log_card.grid_rowconfigure(1, weight=1)
log_header = tk.Frame(log_card, bg=COLORS["surface"])
log_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(13, 9))
log_header.grid_columnconfigure(0, weight=1)
tk.Label(
    log_header,
    text="ACTIVITY",
    bg=COLORS["surface"],
    fg=COLORS["cyan"],
    font=("Segoe UI", 9, "bold"),
).grid(row=0, column=0, sticky="w")
tk.Label(
    log_header,
    text="LIVE OUTPUT",
    bg=COLORS["surface_alt"],
    fg=COLORS["muted"],
    font=("Segoe UI", 8, "bold"),
    padx=9,
    pady=5,
).grid(row=0, column=1, sticky="e")
log_frame = tk.Frame(log_card, bg=COLORS["log"], highlightbackground=COLORS["border"], highlightthickness=1)
log_frame.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 16))
log_frame.grid_columnconfigure(0, weight=1)
log_frame.grid_rowconfigure(0, weight=1)
log_box = tk.Text(
    log_frame,
    height=8,
    bg=COLORS["log"],
    fg="#B8C5D9",
    insertbackground=COLORS["cyan"],
    selectbackground=COLORS["accent"],
    relief=tk.FLAT,
    borderwidth=0,
    padx=12,
    pady=10,
    font=("Consolas", 9),
    wrap=tk.WORD,
)
log_box.grid(row=0, column=0, sticky="nsew")
log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=log_box.yview)
log_scrollbar.grid(row=0, column=1, sticky="ns")
log_box.configure(yscrollcommand=log_scrollbar.set)

root.after(100, flush_log_messages)
root.mainloop()