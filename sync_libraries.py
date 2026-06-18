import os
import subprocess
import tarfile
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding='utf-8')
from pathlib import Path
from dotenv import load_dotenv

# Load configurations
project_dir = Path(r"C:\Users\Adarsh Singh\PSudofy")
load_dotenv(project_dir / ".env")

SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", r"C:\Users\Adarsh Singh\Downloads\imp\ssh-key-2026-05-28.key")
SSH_HOST = os.getenv("SSH_HOST", "ubuntu@161.118.165.241")
REMOTE_DIR = os.getenv("REMOTE_DIR", "~/PSudofy")
MUSIC_FOLDER = os.getenv("MUSIC_FOLDER", "./music")

local_music_dir = project_dir / "music"
local_tar_path = project_dir / "music_sync.tar"
remote_dir_resolved = REMOTE_DIR.replace("~", "/home/ubuntu")
remote_music_path = f"{remote_dir_resolved}/{MUSIC_FOLDER}"
remote_tar_path = f"{remote_dir_resolved}/music_sync.tar"

# List of 28 local-only files (relative paths)
local_only_files = [
    "Jagjit Singh/Kal Chaudhvin Ki Raat Thi - Jagjit Singh.{ext}.mp3",
    "Kumar Sanu/Jab Koi Baat Bigad Jaye (From 'Jurm') - Kumar Sanu.{ext}.mp3",
    "Mamta Sharma/Fevicol Se - Mamta Sharma.{ext}.mp3",
    "Mamta Sharma/Munni Badnaam - Mamta Sharma.{ext}.mp3",
    "Meet Bros Anjjan/Chittiyaan Kalaiyaan - Meet Bros Anjjan.{ext}.mp3",
    "Mohammed Irfan/Banjaara - Mohammed Irfan.{ext}.mp3",
    "Mubarak Begum/Mujhko Apne Gale Laga Lo - Mubarak Begum.{ext}.mp3",
    "Mukesh/Main Pal Do Pal Ka Shair Hoon - Mukesh.{ext}.mp3",
    "Nusrat Fateh Ali Khan/Afreen Afreen - Nusrat Fateh Ali Khan.{ext}.mp3",
    "Pritam/Channa Mereya - Pritam.{ext}.mp3",
    "Pritam/Raabta - Pritam.{ext}.mp3",
    "Pritam/Tu Hi Mera - Pritam.{ext}.mp3",
    "Pritam/Tum Jo Aaye - Pritam.{ext}.mp3",
    "Sachin-Jigar/Dance Basanti - Sachin-Jigar.{ext}.mp3",
    "Sachin-Jigar/Gulabi - Sachin-Jigar.{ext}.mp3",
    "Sachin-Jigar/Jeene Laga Hoon - Sachin-Jigar.{ext}.mp3",
    "Salim Merchant/Aye Khuda - Salim Merchant.{ext}.mp3",
    "Salim–Sulaiman/Ainvayi Ainvayi - Salim–Sulaiman.{ext}.mp3",
    "Salman Khan/Hangover - Salman Khan.{ext}.mp3",
    "Shankar-Ehsaan-Loy/Ishq Di Baajiyaan - Shankar-Ehsaan-Loy.{ext}.mp3",
    "Shankar-Ehsaan-Loy/Zinda - Shankar-Ehsaan-Loy.{ext}.mp3",
    "Sonu Nigam/Pal Pal Har Pal (From 'Lage Raho Munna Bhai') - Sonu Nigam.{ext}.mp3",
    "Sultana/Patakha Guddi - Sultana.{ext}.mp3",
    "Suman Kalyanpur/Aajkal Tere Mere Pyar Ke Charche (From 'Brahmachari') - Suman Kalyanpur.{ext}.mp3",
    "Talat Mahmood/Itna Na Mujhse Tu Pyar Badha - Talat Mahmood.{ext}.mp3",
    "Tarun Sagar/Oh Girl You're Mine . - Tarun Sagar.{ext}.mp3",
    "Udit Narayan/Papa Kahte Hain - Udit Narayan.{ext}.mp3",
    "Yash Narvekar/Muqabla (From 'Street Dancer 3D') - Yash Narvekar.{ext}.mp3"
]

# List of size-mismatched files where local is healthy and remote is truncated
truncated_on_remote = [
    "Mansheel Gujral/Channa Ve - From 'Bhoot - Part One- The Haunted Ship' - Mansheel Gujral.{ext}.mp3",
    "Jagjit Singh/Hothon Se Chhu Lo Tum - From 'Prem Geet' - Jagjit Singh.{ext}.mp3"
]

# Total files we want to upload to server first
files_to_upload = local_only_files + truncated_on_remote

def run_ssh(cmd):
    args = [
        "ssh", "-i", SSH_KEY_PATH, 
        "-o", "StrictHostKeyChecking=no", 
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        SSH_HOST, cmd
    ]
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise Exception(f"SSH command failed with code {res.returncode}. Error:\n{res.stderr}")
    return res.stdout

def run_scp_upload(local_path, remote_path):
    args = [
        "scp", "-i", SSH_KEY_PATH, 
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        str(local_path), f"{SSH_HOST}:{remote_path}"
    ]
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise Exception(f"SCP upload failed. Error:\n{res.stderr}")

def run_scp_download(remote_path, local_path):
    args = [
        "scp", "-i", SSH_KEY_PATH, 
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        f"{SSH_HOST}:{remote_path}", str(local_path)
    ]
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise Exception(f"SCP download failed. Error:\n{res.stderr}")

def main():
    print("==================================================")
    print("PSudofy Bidirectional Sync Starting...")
    print("==================================================")
    
    # ── STEP 1: Upload Files ──────────────────────────────────────────────────
    print("\n[Step 1/5] Uploading local-only and healthy mismatched files...")
    
    # Ensure parent folders exist on remote server
    unique_artists = set(Path(f).parent.as_posix() for f in files_to_upload)
    print(f"Creating {len(unique_artists)} artist directories on remote server...")
    
    mkdir_cmds = []
    for artist in sorted(unique_artists):
        mkdir_cmds.append(f"mkdir -p \"{remote_music_path}/{artist}\"")
    
    # Run directory creation in one go
    run_ssh(" && ".join(mkdir_cmds))
    print("✓ Remote artist directories ready.")
    
    # Upload files
    uploaded_count = 0
    for rel_path in files_to_upload:
        l_file = local_music_dir / rel_path
        r_file = f"{remote_music_path}/{rel_path}"
        if not l_file.exists():
            print(f"  [⚠️] Local file not found: {rel_path} - skipping.")
            continue
            
        print(f"  Uploading: {rel_path} ({l_file.stat().st_size/1024/1024:.2f} MB)...")
        try:
            run_scp_upload(l_file, r_file)
            uploaded_count += 1
        except Exception as e:
            print(f"  [❌] Failed to upload {rel_path}: {e}")
            sys.exit(1)
            
    print(f"✓ Step 1 Complete: Uploaded {uploaded_count} files.")
    
    # ── STEP 2: Package Remote Library ────────────────────────────────────────
    print("\n[Step 2/5] Packaging remote music library into a tar archive on the server...")
    tar_cmd = f"tar -cf {remote_tar_path} -C {remote_music_path} ."
    try:
        run_ssh(tar_cmd)
        print("✓ Remote tar archive created successfully.")
    except Exception as e:
        print(f"[❌] Failed to package remote files: {e}")
        sys.exit(1)
        
    # ── STEP 3: Download Tar ──────────────────────────────────────────────────
    print("\n[Step 3/5] Downloading the tar archive to local PC...")
    print("Please wait, this will transfer the entire library (approx 3.6 GB)...")
    import time
    max_retries = 3
    download_ok = False
    for attempt in range(1, max_retries + 1):
        print(f"  Attempt {attempt}/{max_retries}...")
        # Clean up local partial file if exists
        if local_tar_path.exists():
            try:
                os.remove(local_tar_path)
            except Exception as e:
                print(f"  [⚠️] Could not remove partial local tar: {e}")
        try:
            run_scp_download(remote_tar_path, local_tar_path)
            print(f"✓ Tar archive downloaded successfully to: {local_tar_path}")
            download_ok = True
            break
        except Exception as e:
            print(f"  [⚠️] Download failed on attempt {attempt}: {e}")
            if attempt < max_retries:
                print("  Waiting 15 seconds before retrying...")
                time.sleep(15)
                
    if not download_ok:
        print("[❌] Max retries reached. Failed to download the library.")
        # Clean up remote tar anyway
        try: run_ssh(f"rm {remote_tar_path}")
        except: pass
        sys.exit(1)
        
    # ── STEP 4: Extract Tar ───────────────────────────────────────────────────
    print("\n[Step 4/5] Extracting archive locally to sync all missing songs...")
    try:
        # Use python tarfile to extract
        with tarfile.open(local_tar_path, "r:") as tar:
            # We want to extract into the local music directory
            tar.extractall(path=local_music_dir)
        print("✓ All files extracted successfully.")
    except Exception as e:
        print(f"[❌] Failed to extract archive locally: {e}")
        sys.exit(1)
        
    # ── STEP 5: Clean Up ───────────────────────────────────────────────────────
    print("\n[Step 5/5] Cleaning up temporary archive files...")
    
    # Remove local tar
    if local_tar_path.exists():
        try:
            os.remove(local_tar_path)
            print("✓ Local temporary archive deleted.")
        except Exception as e:
            print(f"  [⚠️] Failed to delete local archive: {e}")
            
    # Remove remote tar
    try:
        run_ssh(f"rm {remote_tar_path}")
        print("✓ Remote temporary archive deleted.")
    except Exception as e:
        print(f"  [⚠️] Failed to delete remote archive: {e}")
        
    print("\n==================================================")
    print("PSudofy Bidirectional Sync Complete!")
    print("==================================================")

if __name__ == "__main__":
    main()
