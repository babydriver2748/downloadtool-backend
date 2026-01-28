from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import yt_dlp
import os
import glob
import time
import asyncio

app = FastAPI()

# --- DATABASE (In-Memory) ---
download_history = []
# ----------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str

# --- ROBUST UTILS ---
def cleanup_file(path: str):
    """Deletes the file safely without crashing"""
    try:
        # Wait a bit to ensure file is fully sent
        time.sleep(2) 
        if os.path.exists(path):
            os.remove(path)
            print(f"Cleanup: Deleted {path}")
    except Exception as e:
        print(f"Cleanup Warning (Not Critical): {e}")

def update_progress(job_id, d):
    """Updates progress safely"""
    if d['status'] == 'downloading':
        try:
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            
            if total:
                percentage = (downloaded / total) * 100
                size_mb = total / 1024 / 1024
            else:
                percentage = 0
                size_mb = 0

            # Find item safely
            for item in download_history:
                if item["id"] == job_id:
                    item["progress"] = percentage
                    item["status"] = "downloading"
                    item["size"] = f"{size_mb:.1f} MB"
                    break
        except Exception as e:
            # Don't crash if progress fails
            print(f"Progress Error (Ignored): {e}")

# --- DOWNLOAD ENGINE ---
def run_yt_dlp_download(url: str, ydl_opts: dict):
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"yt-dlp internal error: {e}")
        raise e

def download_video_background(url: str, job_id: str, format_id: str):
    print(f"Job {job_id}: Starting...")
    output_folder = "downloads"
    os.makedirs(output_folder, exist_ok=True)
    file_pattern = f"{output_folder}/{job_id}.*"

    # Cleanup old files first
    for f in glob.glob(file_pattern):
        try: os.remove(f)
        except: pass

    def hook_wrapper(d):
        update_progress(job_id, d)

    # ATTEMPT 1: High Quality
    ydl_opts = {
        'outtmpl': f'{output_folder}/{job_id}.%(ext)s',
        'quiet': False,
        'no_warnings': True,
        'overwrites': True,
        'progress_hooks': [hook_wrapper],
        'cookiefile': 'cookies.txt',  # <--- [UPDATED] Uses cookies to bypass login
    }

    if "mp3" in format_id:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3'}]
    else:
        # Fallback to 'best' if specific resolution fails
        ydl_opts['format'] = 'bestvideo+bestaudio/best'

    success = False
    final_filename = None

    try:
        run_yt_dlp_download(url, ydl_opts)
        
        # Check if file exists
        found_files = [f for f in glob.glob(file_pattern) if not f.endswith('.part')]
        if found_files and os.path.getsize(found_files[0]) > 0:
            success = True
            final_filename = found_files[0]
            print(f"Job {job_id}: Success -> {final_filename}")
    except Exception as e:
        print(f"Job {job_id}: Failed Attempt 1: {e}")

    # ATTEMPT 2: Safe Mode (If High Quality Failed)
    if not success and "mp3" not in format_id:
        print(f"Job {job_id}: Retrying in Safe Mode (format=18)...")
        ydl_opts['format'] = '18' # 360p/Standard MP4 (Always works)
        try:
            run_yt_dlp_download(url, ydl_opts)
            found_files = [f for f in glob.glob(file_pattern) if not f.endswith('.part')]
            if found_files:
                success = True
                final_filename = found_files[0]
        except Exception as e:
            print(f"Job {job_id}: Failed Safe Mode: {e}")

    # UPDATE STATUS (CRASH PROOF)
    try:
        for item in download_history:
            if item["id"] == job_id:
                if success:
                    item["status"] = "completed"
                    item["progress"] = 100
                    item["file_path"] = final_filename
                else:
                    item["status"] = "failed"
                break
    except Exception as e:
        print(f"Error updating final status: {e}")

# --- ENDPOINTS ---
@app.get("/")
def read_root():
    return {"status": "Backend is running"}

@app.get("/api/history")
def get_history():
    return download_history

@app.get("/api/file/{job_id}")
def download_file(job_id: str, background_tasks: BackgroundTasks):
    output_folder = "downloads"
    files = glob.glob(f"{output_folder}/{job_id}.*")
    valid_files = [f for f in files if not f.endswith('.part')]
    
    if not valid_files:
        raise HTTPException(status_code=404, detail="File not found")
    
    file_path = valid_files[0]
    filename = os.path.basename(file_path)

    # Add cleanup task (This runs AFTER response is sent)
    background_tasks.add_task(cleanup_file, file_path)
    
    return FileResponse(
        path=file_path, 
        filename=f"video_download_{filename}", 
        media_type='application/octet-stream'
    )

@app.post("/api/analyze")
async def analyze_video(request: AnalyzeRequest):
    print(f"Analyzing: {request.url}")
    try:
        # [UPDATED] Added cookies here as well
        ydl_opts = {
            'quiet': True, 
            'no_warnings': True,
            'cookiefile': 'cookies.txt' 
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(request.url, download=False)
            
            def get_size_str(bytes_val):
                if not bytes_val: return "Unknown"
                return f"{bytes_val / 1024 / 1024:.1f} MB"

            # Helper to find specific format size
            formats = info.get('formats', [])
            def find_size(height=None, is_audio=False):
                candidates = []
                for f in formats:
                    if is_audio and f.get('vcodec') == 'none': candidates.append(f)
                    elif f.get('height') == height: candidates.append(f)
                
                if not candidates: return 0
                sizes = [f.get('filesize') or f.get('filesize_approx') or 0 for f in candidates]
                return max(sizes) if sizes else 0

            audio_bytes = find_size(is_audio=True)
            video_720 = find_size(height=720)
            video_480 = find_size(height=480)
            best_bytes = info.get('filesize') or info.get('filesize_approx') or 0
            
            # Estimate combined sizes
            est_audio = audio_bytes if audio_bytes > 0 else 3*1024*1024
            final_720 = (video_720 + est_audio) if video_720 else 0
            final_480 = (video_480 + est_audio) if video_480 else 0

            if best_bytes == 0: best_bytes = max(final_720, final_480, 5*1024*1024)

            return {
                "status": "success",
                "video_title": info.get('title', 'Unknown Title'),
                "thumbnail": info.get('thumbnail', 'https://via.placeholder.com/640x360'),
                "sizes": {
                    "best": get_size_str(best_bytes),
                    "720p": get_size_str(final_720) if final_720 else "N/A",
                    "480p": get_size_str(final_480) if final_480 else "N/A",
                    "mp3": get_size_str(audio_bytes)
                }
            }
    except Exception as e:
        print(f"Analyze Error: {e}")
        # Return generic data instead of crashing
        return {
            "status": "success",
            "video_title": "Video Found",
            "thumbnail": "https://via.placeholder.com/640x360",
            "sizes": {"best": "Unknown", "720p": "Unknown", "480p": "Unknown", "mp3": "Unknown"}
        }

@app.post("/api/download")
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    # Create job immediately
    new_job_id = str(len(download_history) + 1)
    new_record = {
        "id": new_job_id,
        "title": "Processing...",
        "platform": "YouTube",
        "thumbnail": "https://via.placeholder.com/640x360",
        "thumbnailAlt": "Video",
        "format": request.format_id,
        "quality": "Best",
        "size": "Calculating...",
        "progress": 0,
        "status": "downloading", 
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    
    # Add to history
    download_history.insert(0, new_record)
    
    # Start background task
    background_tasks.add_task(download_video_background, request.url, new_job_id, request.format_id)
    
    return {"status": "processing", "job_id": new_job_id}