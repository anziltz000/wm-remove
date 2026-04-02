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

# --- Config ---
BASE_DIR       = Path("/app/storage")
UPLOAD_DIR     = BASE_DIR / "uploads"
WATERMARK_DIR  = BASE_DIR / "watermark"
PROCESSED_DIR  = BASE_DIR / "processed"

for d in (UPLOAD_DIR, WATERMARK_DIR, PROCESSED_DIR):
    d.mkdir(parents=True, exist_ok=True)

TOKEN_PATH       = BASE_DIR / "token.json"
DRIVE_FOLDER_ID  = os.getenv("DRIVE_FOLDER_ID", "")
MAX_AGE_DAYS     = 7

processing_lock = asyncio.Lock()
jobs: dict[str, dict] = {}

app = FastAPI(title="WREM Pipeline")
templates = Jinja2Templates(directory="templates")

# --- Google Drive Helpers ---
def get_drive_service():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"token.json missing at {TOKEN_PATH}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), ["https://www.googleapis.com/auth/drive.file"])
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
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

# --- FFmpeg ---
async def run_ffmpeg(input_path: Path, logo_path: Path, output_path: Path) -> tuple[bool, str]:
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path), "-i", str(logo_path),
        "-filter_complex", "[1:v]colorkey=0x00FF00:0.1:0.1,tpad=stop_mode=clone:stop=-1[keyed];[keyed][0:v]scale2ref=w=iw:h=ih[logo][base];[base][logo]overlay=0:0:shortest=1",
        "-c:v", "libx264", "-crf", "18", "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "copy",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    return (proc.returncode == 0, stderr.decode(errors="replace"))

# --- Worker ---
async def process_job(job_id: str, wm_path: Path):
    async with processing_lock:
        job = jobs.get(job_id)
        if not job: return
        for entry in job["files"]:
            src = UPLOAD_DIR / job_id / entry["name"]
            out = PROCESSED_DIR / job_id / f"(Wrem)_{entry['name']}"
            out.parent.mkdir(parents=True, exist_ok=True)
            
            entry["status"] = "processing"
            ok, err = await run_ffmpeg(src, wm_path, out)
            
            if ok and DRIVE_FOLDER_ID:
                entry["status"] = "uploading"
                try:
                    upload_to_drive(out, DRIVE_FOLDER_ID)
                    out.unlink(missing_ok=True)
                    entry["status"] = "uploaded"
                except Exception as e:
                    entry["status"] = "failed"
                    entry["error"] = str(e)
            elif ok:
                entry["status"] = "done"
            else:
                entry["status"] = "failed"
                entry["error"] = err[:200]
            
            if src.exists(): src.unlink()

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/process")
async def start_process(
    background_tasks: BackgroundTasks,
    watermark: UploadFile = File(...),
    videos: list[UploadFile] = File(...)
):
    job_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    wm_path = WATERMARK_DIR / f"{job_id}_{watermark.filename}"
    with open(wm_path, "wb") as f:
        shutil.copyfileobj(watermark.file, f)

    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    file_entries = []
    for vid in videos:
        name = sanitise(vid.filename) + ".mp4"
        with open(job_dir / name, "wb") as f:
            shutil.copyfileobj(vid.file, f)
        file_entries.append({"name": name, "status": "pending", "error": ""})

    jobs[job_id] = {"files": file_entries}
    background_tasks.add_task(process_job, job_id, wm_path)
    return JSONResponse({"redirect_url": f"/status/{job_id}"})

@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status_page(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job: return RedirectResponse(url="/")
    return templates.TemplateResponse("index.html", {"request": request, "job_id": job_id, "job": job})

@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    return jobs.get(job_id, {"error": "not found"})

@app.post("/flush")
async def flush_storage():
    for d in (UPLOAD_DIR, PROCESSED_DIR, WATERMARK_DIR):
        for p in d.rglob("*"): 
            if p.is_file(): p.unlink(missing_ok=True)
    jobs.clear()
    return {"message": "Storage flushed"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
