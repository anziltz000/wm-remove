import os
import re
import shutil
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# Google Drive
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Dirs ────────────────────────────────────────────────────────────────────
BASE_DIR       = Path("/app/storage")
UPLOAD_DIR     = BASE_DIR / "uploads"
WATERMARK_DIR  = BASE_DIR / "watermark"
PROCESSED_DIR  = BASE_DIR / "processed"

for d in (UPLOAD_DIR, WATERMARK_DIR, PROCESSED_DIR):
    d.mkdir(parents=True, exist_ok=True)

TOKEN_PATH       = BASE_DIR / "token.json"
DRIVE_FOLDER_ID  = os.getenv("DRIVE_FOLDER_ID", "")
MAX_AGE_DAYS     = 7

jobs: dict[str, dict] = {}
app = FastAPI(title="WREM — GreenScreen Freeze-Frame Pipeline")
templates = Jinja2Templates(directory="templates")

# ─── Google Drive Helper ─────────────────────────────────────────────────────
def get_drive_service():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"token.json not found at {TOKEN_PATH}")
    
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), scopes=["https://www.googleapis.com/auth/drive.file"])
    
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())
            
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def upload_to_drive(filepath: Path, folder_id: str) -> str:
    service = get_drive_service()
    meta = {"name": filepath.name, "parents": [folder_id]}
    media = MediaFileUpload(str(filepath), mimetype="video/mp4", resumable=True)
    file = service.files().create(body=meta, media_body=media, fields="id").execute()
    return file.get("id", "")

def sanitise(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[^\w\s\-]", "", stem)
    stem = re.sub(r"\s+", "_", stem).strip("_")
    return stem or "video"

# ─── FFmpeg Green Screen + Freeze Last Frame ─────────────────────────────────
async def run_ffmpeg(input_path: Path, logo_video_path: Path, output_path: Path) -> tuple[bool, str]:
    """
    1. Removes green screen (chromakey)
    2. Pads the logo video by cloning the last frame indefinitely (tpad)
    3. Scales logo to match main video size
    4. Overlays onto main video
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-i", str(logo_video_path),
        "-filter_complex", 
        # 1. Chromakey: Remove green (0x00FF00)
        # 2. Tpad: Clone the last frame (stop_mode=clone) indefinitely (stop=-1)
        "[1:v]colorkey=0x00FF00:0.1:0.1,tpad=stop_mode=clone:stop=-1[keyed];" 
        # 3. Scale keyed/padded logo to match the base video resolution
        "[keyed][0:v]scale2ref=w=iw:h=ih[logo][base];"
        # 4. Overlay logo on base, stop when the main video (shortest=1) finishes
        "[base][logo]overlay=0:0:shortest=1",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "slow",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "copy",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return False, stderr.decode(errors="replace")
    return True, ""

# ─── Background Worker ───────────────────────────────────────────────────────
async def process_job(job_id: str, watermark_path: Path):
    job = jobs[job_id]
    for entry in job["files"]:
        name    = entry["name"]
        src     = UPLOAD_DIR / job_id / name
        safe    = sanitise(name)
        out_name = f"(Wrem)_{safe}_og_{Path(name).stem}.mp4"
        out     = PROCESSED_DIR / job_id / out_name

        out.parent.mkdir(parents=True, exist_ok=True)
        entry["status"] = "processing"
        log.info("[%s] FFmpeg processing (Freeze-Frame) → %s", job_id, out_name)

        ok, err = await run_ffmpeg(src, watermark_path, out)
        if not ok:
            entry["status"] = "failed"
            entry["error"]  = err[:300]
            log.error("[%s] FFmpeg failed: %s", job_id, err[:200])
            continue

        if DRIVE_FOLDER_ID:
            try:
                entry["status"] = "uploading"
                drive_id = upload_to_drive(out, DRIVE_FOLDER_ID)
                log.info("[%s] Uploaded → Drive ID: %s", job_id, drive_id)
                out.unlink(missing_ok=True)
                entry["status"] = "uploaded"
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"]  = str(exc)[:300]
                log.error("[%s] Upload failed: %s", job_id, exc)
        else:
            entry["status"] = "done"

        src.unlink(missing_ok=True)
    log.info("[%s] Job completely finished.", job_id)

# ─── Housekeeping & Routing (Standard) ───────────────────────────────────────
async def cleanup_old_files():
    while True:
        cutoff = datetime.utcnow() - timedelta(days=MAX_AGE_DAYS)
        for d in (UPLOAD_DIR, PROCESSED_DIR, WATERMARK_DIR):
            for path in d.rglob("*"):
                if path.is_file():
                    mtime = datetime.utcfromtimestamp(path.stat().st_mtime)
                    if mtime < cutoff:
                        path.unlink(missing_ok=True)
        await asyncio.sleep(3600)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_old_files())

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/process")
async def start_process(
    background_tasks: BackgroundTasks,
    watermark: UploadFile = File(...),
    videos: list[UploadFile] = File(...),
):
    job_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    wm_path = WATERMARK_DIR / f"{job_id}_{watermark.filename}"
    with open(wm_path, "wb") as f:
        shutil.copyfileobj(watermark.file, f)

    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    file_entries = []
    for vid in videos:
        safe_name = sanitise(vid.filename) + ".mp4"
        dest = job_dir / safe_name
        with open(dest, "wb") as f:
            shutil.copyfileobj(vid.file, f)
        file_entries.append({"name": safe_name, "status": "pending", "error": ""})

    jobs[job_id] = {"files": file_entries, "started": datetime.utcnow().isoformat()}
    background_tasks.add_task(process_job, job_id, wm_path)
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)

@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status_page(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job: return HTMLResponse("Job not found", status_code=404)
    return templates.TemplateResponse("index.html", {"request": request, "job_id": job_id, "job": job})

@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    job = jobs.get(job_id)
    return JSONResponse(job) if job else JSONResponse({"error": "not found"}, status_code=404)

@app.post("/flush")
async def flush_storage():
    count = 0
    for d in (UPLOAD_DIR, PROCESSED_DIR, WATERMARK_DIR):
        for p in d.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True); count += 1
    jobs.clear()
    return JSONResponse({"message": f"Flushed {count} files."})

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
