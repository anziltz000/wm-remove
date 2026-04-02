# WREM — Watermark · Encode · Upload Pipeline

Batch-watermark vertical videos, encode for social media, auto-upload to Google Drive.

---

## Project layout

```
wrem/
├── main.py               # FastAPI app
├── templates/
│   └── index.html        # Dark-themed UI
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example          # Copy → .env and fill in
├── .gitignore
└── README.md
```

## File-naming convention

Output files are named:

```
(Wrem)_<sanitised_name>_og_<original_stem>.mp4
```

Example: `(Wrem)_my_clip_og_My Clip (1).mp4`

---

## One-time setup (Google Drive Service Account)

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services → Enable APIs** → enable **Google Drive API**.
2. **APIs & Services → Credentials → Create Credentials → Service Account**.
   - Name it anything (e.g. `wrem-uploader`).
3. Open the service account → **Keys → Add Key → JSON** → download. Rename to `credentials.json`.
4. In Google Drive, **right-click your upload folder → Share** → paste the service account email (`something@project.iam.gserviceaccount.com`) → give **Editor** access.
5. Copy the folder ID from the Drive URL:
   `https://drive.google.com/drive/folders/`**`THIS_IS_YOUR_FOLDER_ID`**

---

## Local → GitHub → Oracle Cloud deployment

### Step 1 — Prepare repo locally

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/wrem.git
git push -u origin main
```

> `credentials.json` and `.env` are in `.gitignore` — they will **not** be pushed.

---

### Step 2 — Oracle Cloud VM

SSH into your VM:

```bash
ssh ubuntu@<your-vm-ip>
```

Install Docker & Compose (if not already done):

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
newgrp docker
```

---

### Step 3 — Clone repo on VM

```bash
git clone https://github.com/YOUR_USERNAME/wrem.git
cd wrem
```

---

### Step 4 — Drop secrets onto VM

From your **local machine** (not inside ssh), copy the credentials file:

```bash
scp credentials.json ubuntu@<your-vm-ip>:~/wrem/credentials.json
```

Then on the VM, create your `.env`:

```bash
cp .env.example .env
nano .env
# → paste your real DRIVE_FOLDER_ID and save
```

---

### Step 5 — Build and run

```bash
docker compose up -d --build
```

Check logs:

```bash
docker compose logs -f wrem
```

---

### Step 6 — Cloudflare Tunnel

In your Cloudflare Zero Trust dashboard, point the tunnel to:

```
http://localhost:8000
```

Your app is now live at `https://wrem.yourdomain.com` (or whatever subdomain you configured).

---

## Updating after code changes

```bash
# On local machine
git add . && git commit -m "update" && git push

# On VM
cd ~/wrem
git pull
docker compose up -d --build
```

---

## Manual flush

Hit the **⚠ Flush Storage** button in the UI, or call:

```bash
curl -X POST http://localhost:8000/flush
```

---

## Environment variables

| Variable         | Description                          | Required |
|------------------|--------------------------------------|----------|
| `DRIVE_FOLDER_ID`| Google Drive target folder ID        | Yes      |

---

## Notes

- FFmpeg encoding flags: `-c:v libx264 -crf 18 -preset slow -pix_fmt yuv420p -movflags +faststart -c:a copy`
- Files older than **7 days** are automatically purged (runs hourly).
- The job status page **auto-polls** every 2.5 seconds via `/api/status/{job_id}`.
- In-memory job registry resets on container restart (by design — lightweight, no DB required).
