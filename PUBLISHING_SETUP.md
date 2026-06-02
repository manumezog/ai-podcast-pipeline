# Publishing Setup Guide

Two accounts needed, both free. Takes ~30 minutes total.

---

## 1. Cloudflare R2 (audio file hosting)

**Create account:** https://dash.cloudflare.com/sign-up (free, no credit card needed for R2 free tier)

**R2 free tier:** 10 GB storage, 1 million Class A operations/month — more than enough for a daily podcast.

### Steps:

1. Log in → left sidebar → **R2 Object Storage** → **Create bucket**
   - Name: `podcast` (or whatever you put in `CLOUDFLARE_R2_BUCKET_NAME`)
   - Region: automatic

2. Make the bucket public (so audio URLs work without auth):
   - Open your bucket → **Settings** tab → **Public Access** → **Allow Access** → confirm

3. Note your **Public R2.dev subdomain** — looks like `pub-xxxxxxxxxxxxxxxx.r2.dev`
   - This becomes `CLOUDFLARE_R2_PUBLIC_URL=https://pub-xxxxxxxxxxxxxxxx.r2.dev`

4. Get your **Account ID**: top-right of any Cloudflare page or R2 overview → sidebar shows it
   - This becomes `CLOUDFLARE_R2_ACCOUNT_ID`

5. Create API credentials:
   - R2 overview → **Manage R2 API Tokens** → **Create API Token**
   - Permissions: **Object Read & Write**
   - Scope: **Specific bucket** → select `podcast`
   - Copy **Access Key ID** → `CLOUDFLARE_R2_ACCESS_KEY_ID`
   - Copy **Secret Access Key** → `CLOUDFLARE_R2_SECRET_ACCESS_KEY` (only shown once!)

6. Upload your podcast artwork:
   - Prepare a 1400×1400 px JPG image (required by Apple Podcasts and Spotify)
   - Upload it to your R2 bucket as `artwork.jpg`
   - Public URL: `https://pub-xxxxxxxxxxxxxxxx.r2.dev/artwork.jpg`
   - Set `PODCAST_ARTWORK_URL` to this URL

---

## 2. GitHub (RSS feed hosting via GitHub Pages)

**Create account:** https://github.com/join (free)

### Steps:

1. Create a new **public** repository:
   - https://github.com/new
   - Name: `podcast-feed` (or anything you prefer)
   - Visibility: **Public** (required for GitHub Pages)
   - Initialize with a README: yes

2. Enable GitHub Pages:
   - Repository → **Settings** → **Pages**
   - Source: **Deploy from a branch**
   - Branch: `main` / `(root)`
   - Save

3. Your RSS feed URL will be:
   `https://yourusername.github.io/podcast-feed/feed.xml`
   - Set `PODCAST_RSS_URL` to this URL
   - Set `GITHUB_REPO=yourusername/podcast-feed`

4. Create a Personal Access Token:
   - GitHub → top-right avatar → **Settings** → **Developer settings**
   - **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
   - Token name: `podcaster`
   - Expiration: 1 year (or no expiration)
   - Repository access: **Only select repositories** → select `podcast-feed`
   - Permissions → **Contents**: **Read and write**
   - Generate → copy the token
   - Set `GITHUB_TOKEN` to this value

---

## 3. Submit RSS feed to platforms (one-time, after first episode)

Once you've run `python pipeline_runner.py --publish` for the first time and your RSS feed is live at the GitHub Pages URL, submit it to each platform once:

| Platform | Submission URL |
|---|---|
| **Spotify** | https://podcasters.spotify.com → Add existing podcast → paste RSS URL |
| **Apple Podcasts** | https://podcastsconnect.apple.com → + → paste RSS URL |
| **Amazon Music** | https://podcasters.amazon.com → Submit → paste RSS URL |
| **YouTube Music** | https://music.youtube.com/podcasts (via RSS import) |

After submission, each platform polls your RSS feed automatically. New episodes appear within a few hours of publishing.

---

## 4. Running the publisher

```bash
# Publish today's episode
python pipeline_runner.py --publish

# Full pipeline + publish in one command
python pipeline_runner.py --publish

# Test the publisher without actually uploading
python modules/publisher.py --dry-run

# Publish a specific date
python modules/publisher.py --date 2026-06-02
```

---

## 5. .env checklist

```
CLOUDFLARE_R2_ACCOUNT_ID=...
CLOUDFLARE_R2_ACCESS_KEY_ID=...
CLOUDFLARE_R2_SECRET_ACCESS_KEY=...
CLOUDFLARE_R2_BUCKET_NAME=podcast
CLOUDFLARE_R2_PUBLIC_URL=https://pub-xxxx.r2.dev
GITHUB_TOKEN=...
GITHUB_REPO=yourusername/podcast-feed
PODCAST_ARTWORK_URL=https://pub-xxxx.r2.dev/artwork.jpg
PODCAST_RSS_URL=https://yourusername.github.io/podcast-feed/feed.xml
PODCAST_DESCRIPTION=A daily podcast covering the latest in robotics and automation.
```
