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

# Google Drive - Updated for OAuth Token Authentication
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Directories & Config ────────────────────────────────────────────────────
BASE_DIR       = Path("/app/storage")
UPLOAD_DIR     = BASE_DIR / "uploads"
WATERMARK_DIR  = BASE_DIR / "watermark"
PROCESSED_DIR  = BASE_DIR / "processed"

# Create directories if they don't exist
for d in (UPLOAD_DIR, WATERMARK_DIR, PROCESSED_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Pointing to the new OAuth token inside the persistent volume
TOKEN_PATH       = BASE_DIR / "token.json"

# Read folder ID from environment, or default to empty
DRIVE_FOLDER_ID  = os.getenv("DRIVE_FOLDER_ID", "")
MAX_AGE_DAYS     = 7

# ─── Job Registry ────────────────────────────────────────────────────────────
# Structure: { job_id: { "files": [ { "name", "status", "error" } ], "started": "..." } }
jobs: dict[str, dict] = {}

app = FastAPI(title="WREM — Watermark & Upload Pipeline")
templates = Jinja2Templates(directory="templates")

# ─── Google Drive Helper (OAuth Token Version) ───────────────────────────────
def get_drive_service():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"token.json not found at {TOKEN_PATH}! Please run the auth.py script.")
    
    creds = Credentials.from_authorized_user_file(
        str(TOKEN_PATH), 
        scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    
    # Automatically refresh the token if it has expired
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

# ─── Filename Sanitiser ──────────────────────────────────────────────────────
def sanitise(name: str) -> str:
    stem = Path(name).stem
    # Remove anything that isn't a word character, space, or dash
    stem = re.sub(r"[^\w\s\-]", "", stem)
    # Replace spaces with underscores
    stem = re.sub(r"\s+", "_", stem).strip("_")
    return stem or "video"

# ─── FFmpeg Processing ───────────────────────────────────────────────────────
async def run_ffmpeg(input_path: Path, watermark_path: Path, output_path: Path) -> tuple[bool, str]:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-i", str(watermark_path),
        # Dynamically scale watermark to match video width/height, then apply overlay at 0:0
        "-filter_complex", "[1:v][0:v]scale2ref=w=iw:h=ih[wm][vid];[vid][wm]overlay=0:0",
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
        # Prefix the output file with (Wrem)
        out_name = f"(Wrem)_{safe}_og_{Path(name).stem}.mp4"
        out     = PROCESSED_DIR / job_id / out_name

        out.parent.mkdir(parents=True, exist_ok=True)
        entry["status"] = "processing"
        log.info("[%s] FFmpeg processing → %s", job_id, out_name)

        ok, err = await run_ffmpeg(src, watermark_path, out)
        if not ok:
            entry["status"] = "failed"
            entry["error"]  = err[:300]
            log.error("[%s] FFmpeg failed for %s: %s", job_id, name, err[:200])
            continue

        # Upload to Google Drive
        if DRIVE_FOLDER_ID:
            try:
                entry["status"] = "uploading"
                drive_id = upload_to_drive(out, DRIVE_FOLDER_ID)
                log.info("[%s] Uploaded %s → Drive ID: %s", job_id, out_name, drive_id)
                out.unlink(missing_ok=True) # Delete local processed file after upload
                entry["status"] = "uploaded"
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"]  = str(exc)[:300]
                log.error("[%s] Drive upload failed: %s", job_id, exc)
        else:
            # If no Drive ID is provided, just mark it done and keep it locally
            entry["status"] = "done"

        # Remove original uploaded file to save space
        src.unlink(missing_ok=True)

    log.info("[%s] Job completely finished.", job_id)

# ─── Auto-destruct Scheduler ─────────────────────────────────────────────────
async def cleanup_old_files():
    while True:
        cutoff = datetime.utcnow() - timedelta(days=MAX_AGE_DAYS)
        for d in (UPLOAD_DIR, PROCESSED_DIR, WATERMARK_DIR):
            for path in d.rglob("*"):
                if path.is_file():
                    mtime = datetime.utcfromtimestamp(path.stat().st_mtime)
                    if mtime < cutoff:
                        path.unlink(missing_ok=True)
                        log.info("[Cleanup] Auto-deleted old file: %s", path)
        # Run check every hour (3600 seconds)
        await asyncio.sleep(3600) 

@app.on_event("startup")
async def startup_event():
    # Start the background cleanup task when the server starts
    asyncio.create_task(cleanup_old_files())

# ─── Routing ─────────────────────────────────────────────────────────────────
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

    # 1. Save watermark locally
    wm_path = WATERMARK_DIR / f"{job_id}_{watermark.filename}"
    with open(wm_path, "wb") as f:
        shutil.copyfileobj(watermark.file, f)

    # 2. Save all uploaded videos locally
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    file_entries = []
    for vid in videos:
        safe_name = sanitise(vid.filename) + ".mp4"
        dest = job_dir / safe_name
        with open(dest, "wb") as f:
            shutil.copyfileobj(vid.file, f)
        file_entries.append({"name": safe_name, "status": "pending", "error": ""})

    # 3. Register the job
    jobs[job_id] = {"files": file_entries, "started": datetime.utcnow().isoformat()}
    
    # 4. Trigger the background processor
    background_tasks.add_task(process_job, job_id, wm_path)

    # 5. Redirect browser to the status page immediately
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status_page(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        return HTMLResponse("<h2>Job not found.</h2>", status_code=404)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "job_id": job_id,
        "job": job,
    })


@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(job)


@app.post("/flush")
async def flush_storage():
    count = 0
    for d in (UPLOAD_DIR, PROCESSED_DIR, WATERMARK_DIR):
        for p in d.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
                count += 1
    # Clear the active job registry
    jobs.clear()
    return JSONResponse({"deleted": count, "message": f"Flushed {count} files."})


@app.get("/api/jobs")
async def list_jobs():
    summary = {}
    for jid, job in jobs.items():
        total   = len(job["files"])
        done    = sum(1 for f in job["files"] if f["status"] in ("uploaded", "done"))
        failed  = sum(1 for f in job["files"] if f["status"] == "failed")
        summary[jid] = {"total": total, "done": done, "failed": failed, "started": job.get("started")}
    return JSONResponse(summary)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
