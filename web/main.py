import os
import uuid
import shutil
import re
from datetime import datetime
import sys
import asyncio
from typing import Optional, Tuple, Dict

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(APP_ROOT, os.pardir))
# Ensure we can import project modules when running from web/
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from spotify_to_mp3 import process_url
DOWNLOADS_DIR = os.path.join(PROJECT_ROOT, "downloads")

app = FastAPI(title="Spotify to MP3 Web")
app.mount("/static", StaticFiles(directory=os.path.join(APP_ROOT, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(APP_ROOT, "templates"))

class Job:
    def __init__(self, url: str, out_dir: str, trim: bool, verbose: bool,
                 username: Optional[str] = None, password: Optional[str] = None,
                 twofactor: Optional[str] = None, usenetrc: bool = False):
        self.id = str(uuid.uuid4())
        self.url = url
        self.out_dir = out_dir
        self.trim = trim
        self.verbose = verbose
        # yt-dlp auth (do not log these)
        self.username = username
        self.password = password
        self.twofactor = twofactor
        self.usenetrc = usenetrc
        self.created_at = datetime.utcnow()
        self.returncode: Optional[int] = None
        self.logs: asyncio.Queue[str] = asyncio.Queue()
        self.output_file: Optional[str] = None
        self.current_title: Optional[str] = None
        self.current_index: Optional[int] = None
        self.total: Optional[int] = None
        self.last_status: Optional[str] = None  # downloading|skipped|error|done

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "out_dir": self.out_dir,
            "trim": self.trim,
            "verbose": self.verbose,
            "created_at": self.created_at.isoformat() + "Z",
            "running": self.returncode is None,
            "returncode": self.returncode,
            "output_file": self.output_file,
            "current_title": self.current_title,
            "current_index": self.current_index,
            "total": self.total,
            "last_status": self.last_status,
        }

JOBS: Dict[str, Job] = {}

# Helpers

def python_exec() -> str:
    # Prefer venv python if present
    venv_py = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
    return venv_py if os.path.exists(venv_py) else "python"

async def sse_event_generator(job: Job):
    # Stream logs until job finishes and queue drains
    while True:
        if job.returncode is not None and job.logs.empty():
            break
        try:
            line = await asyncio.wait_for(job.logs.get(), timeout=0.5)
            yield f"data: {line}\n\n"
        except asyncio.TimeoutError:
            # heartbeat
            yield f": keep-alive\n\n"

async def run_job(job: Job):
    os.makedirs(job.out_dir, exist_ok=True)
    await job.logs.put("Preparando…\n")

    # patterns
    re_track = re.compile(r"^\[(\d+)/(\d+)\]\s+(.+)$")
    downloading_announced = False
    loop = asyncio.get_running_loop()

    # Ensure ffmpeg is discoverable if a local folder exists next to project root
    local_ffmpeg = os.path.join(PROJECT_ROOT, "ffmpeg")
    if os.path.isdir(local_ffmpeg):
        os.environ["PATH"] = local_ffmpeg + os.pathsep + os.environ.get("PATH", "")

    # Cookies now are provided explicitly via cookiefile upload o cookies-from-browser.

    all_lines: list[str] = []
    error_seen = False
    skipped_seen = False
    # Per-track log buffer
    current_track_log: list[str] = []

    def sanitize_filename(name: str) -> str:
        return re.sub(r"[\\/:*?\"<>|]", "_", name).strip() or "track"

    def should_emit_minimal(line: str) -> bool:
        s = line.strip()
        if s.startswith("Preparando…"):
            return True
        if s.startswith("Encontradas "):
            return True
        if s.startswith("Destino:"):
            return True
        if re_track.match(s):
            return True
        if s.startswith("Descargando…"):
            return True
        if s.startswith("Descargado") or "Saltado" in s:
            return True
        return False

    seen_hints = {"bot_check": False}

    def log_cb(message: str):
        # Push log line to async queue from worker thread
        try:
            mline = message.rstrip("\n")
            all_lines.append(mline)
            # append to current track buffer as well
            current_track_log.append(mline)
            if job.verbose or should_emit_minimal(mline):
                loop.call_soon_threadsafe(asyncio.create_task, job.logs.put(mline + "\n"))
        except Exception:
            pass
        # Update status heuristics
        stripped = message.strip()
        m = re_track.match(stripped)
        if m:
            try:
                job.current_index = int(m.group(1))
                job.total = int(m.group(2))
            except Exception:
                pass
            job.current_title = m.group(3)
            # Reset per-track buffer starting at the track header
            current_track_log.clear()
            current_track_log.append(stripped)
            job.last_status = "downloading"
            nonlocal downloading_announced
            if not downloading_announced:
                try:
                    # Always show 'Descargando…' in mini console
                    loop.call_soon_threadsafe(asyncio.create_task, job.logs.put("Descargando…\n"))
                except Exception:
                    pass
                downloading_announced = True
            return
        low = stripped.lower()
        if "saltado" in low or "skip" in low:
            job.last_status = "skipped"
            skipped_seen = True
            # Write per-track log immediately for skipped track
            try:
                if job.current_title:
                    fname = f"{sanitize_filename(job.current_title)}.log.txt"
                    with open(os.path.join(job.out_dir, fname), "w", encoding="utf-8") as f:
                        for ln in current_track_log:
                            f.write(ln + "\n")
            except Exception:
                pass
        elif low.startswith("error:") or " error" in low:
            job.last_status = "error"
            error_seen = True
            # Write per-track log immediately for error
            try:
                if job.current_title:
                    fname = f"{sanitize_filename(job.current_title)}.log.txt"
                    with open(os.path.join(job.out_dir, fname), "w", encoding="utf-8") as f:
                        for ln in current_track_log:
                            f.write(ln + "\n")
            except Exception:
                pass
        # Friendly hints (emit once per job)
        if ("sign in to confirm you’re not a bot" in low or "confirm you're not a bot" in low) and not seen_hints["bot_check"]:
            seen_hints["bot_check"] = True
            hint = "Hint: YouTube exige verificación anti-bot. Si aparece este aviso, reintenta más tarde o inicia sesión en YouTube en tu navegador."
            all_lines.append(hint)
            try:
                loop.call_soon_threadsafe(asyncio.create_task, job.logs.put(hint + "\n"))
            except Exception:
                pass

    try:
        await asyncio.to_thread(
            process_url,
            job.url,
            job.out_dir,
            job.trim,
            log_cb,
            job.verbose,
            # auth
            job.username,
            job.password,
            job.twofactor,
            job.usenetrc,
        )
        job.returncode = 0
        job.last_status = "done"
    except Exception as e:
        err = f"ERROR: {e}"
        all_lines.append(err)
        if job.verbose:
            await job.logs.put(err + "\n")
        job.returncode = 1
        job.last_status = "error"

    # Write log file on error or skipped items
    try:
        if job.returncode != 0 or error_seen or skipped_seen:
            log_path = os.path.join(job.out_dir, f"job_{job.id}.log.txt")
            # Filter noisy debug lines if not verbose
            def keep_line(ln: str) -> bool:
                if job.verbose:
                    return True
                s = ln.strip()
                if s.startswith("[debug]"):
                    return False
                if s.startswith("[info]"):
                    return False
                if s.startswith("[youtube]"):
                    return True
                if s.startswith("ERROR:") or s.startswith("WARN:"):
                    return True
                if s.startswith("[cookies]"):
                    return True
                # keep minimal status lines
                return should_emit_minimal(s)
            with open(log_path, "w", encoding="utf-8") as f:
                for ln in all_lines:
                    if keep_line(ln):
                        f.write(ln + "\n")
    except Exception:
        pass

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/start")
async def start_job(
    url: str = Form(...),
    trim: Optional[bool] = Form(False),
    verbose: Optional[bool] = Form(False),
    # yt-dlp auth
    use_auth: Optional[bool] = Form(False),
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    twofactor: Optional[str] = Form(None),
    usenetrc: Optional[bool] = Form(False),
):
    # Per-job output folder to isolate artifacts
    job_temp = Job(url=url.strip(), out_dir="", trim=bool(trim), verbose=bool(verbose))
    job_id = job_temp.id
    out_dir = os.path.join(DOWNLOADS_DIR, f"job_{job_id}")
    # Pre-create job to get id and out dir
    job = Job(
        url=url.strip(),
        out_dir=out_dir,
        trim=bool(trim),
        verbose=bool(verbose),
        username=(username.strip() if (use_auth and username) else None),
        password=(password if (use_auth and password) else None),
        twofactor=(twofactor.strip() if (use_auth and twofactor) else None),
        usenetrc=bool(usenetrc) if use_auth else False,
    )
    JOBS[job.id] = job
    asyncio.create_task(run_job(job))
    return {"job_id": job.id}

@app.get("/status/{job_id}")
async def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return job.to_dict()

@app.get("/logs/{job_id}")
async def logs(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return StreamingResponse(sse_event_generator(job), media_type="text/event-stream")

@app.get("/download/{job_id}")
async def download(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    # Provide a zip of the downloads dir for now (simple)
    if job.returncode is None:
        raise HTTPException(409, "El job aún está en ejecución")
    # Collect mp3 files inside this job folder
    target_dir = job.out_dir if os.path.isdir(job.out_dir) else DOWNLOADS_DIR
    mp3s = []
    for root, _, files in os.walk(target_dir):
        for fn in files:
            if fn.lower().endswith('.mp3'):
                mp3s.append(os.path.join(root, fn))
    if not mp3s:
        raise HTTPException(404, "No hay MP3 generados para este job")
    if len(mp3s) == 1:
        # Return the single MP3 directly
        single = mp3s[0]
        fname = os.path.basename(single)
        return FileResponse(single, filename=fname)
    # Otherwise zip only this job folder
    zpath = os.path.join(DOWNLOADS_DIR, f"job_{job.id}.zip")
    base = os.path.splitext(zpath)[0]
    if os.path.exists(zpath):
        os.remove(zpath)
    shutil.make_archive(base, "zip", target_dir)
    return FileResponse(zpath, filename=f"spotify_mp3_{job.id}.zip")

@app.get("/api/ffmpeg")
async def ffmpeg_check():
    from shutil import which
    return JSONResponse({"ffmpeg": which("ffmpeg") is not None})

 

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
