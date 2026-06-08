# PSudofy Cloud Server Cheat Sheet

Your Navidrome music server is successfully set up and running 24/7 on Oracle Cloud Infrastructure (OCI) Free Tier!

## 🌐 Server Details
* **Public IP Address:** `161.118.165.241`
* **Navidrome URL:** [http://161.118.165.241:4533](http://161.118.165.241:4533)
* **Operating System:** Ubuntu 20.04 LTS (Ampere A1 Flex)
* **Specs:** 4 OCPUs, 24 GB RAM, 200 GB Storage

---

## 🔑 How to SSH Connect to the Server
Run this command in Windows PowerShell to log into your cloud server:
```powershell
ssh -i "C:\Users\Adarsh Singh\Downloads\ssh-key-2026-05-28.key" ubuntu@161.118.165.241
```

---

## 🎵 How to Sync/Upload Music from your PC
If you download new songs on your local PC and want to push them to the cloud, open a **local** PowerShell window and run:
```powershell
scp -i "C:\Users\Adarsh Singh\Downloads\ssh-key-2026-05-28.key" -r "C:\Users\Adarsh Singh\PSudofy\music\*" ubuntu@161.118.165.241:~/PSudofy/music/
```

---

## 📁 Server Folder Structure
All files are located in the user's home directory:
* `~/PSudofy/` - Main project directory
* `~/PSudofy/docker-compose.yml` - Docker configuration
* `~/PSudofy/data/` - Navidrome user data & database (persisted)
* `~/PSudofy/music/` - Uploaded MP3 music files

---

## 🐳 Docker Management Commands
Run these commands **inside the SSH session** under `~/PSudofy`:

* **Start Navidrome:**
  ```bash
  sudo docker-compose up -d
  ```
* **Stop Navidrome:**
  ```bash
  sudo docker-compose down
  ```
* **Restart Navidrome:**
  ```bash
  sudo docker-compose restart
  ```
* **View Navidrome Logs:**
  ```bash
  sudo docker-compose logs -f
  ```
