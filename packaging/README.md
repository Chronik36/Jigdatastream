# BotTrack

BotTrack reads your Gmail inboxes with an AI model that runs on your own computer and keeps track of every
order (tracking, address, delivery dates, status), your botting spend (proxies, bots, cook groups, captcha,
servers), checkouts from your bots' Discord webhooks, and your profit. Your email never leaves your computer.

## Download

Get the files from the **latest release**: https://github.com/Chronik36/BotTrack-releases/releases/latest

| You have | Download |
| --- | --- |
| Mac (Apple silicon) | `BotTrack.dmg` |
| Windows PC | `BotTrack-Setup.exe` |
| Android phone | `BotTrack-android-….apk` |
| iPhone | nothing to download, see below |

## Install

**Mac**
1. Open `BotTrack.dmg` and drag BotTrack into Applications.
2. Open BotTrack. The first time, macOS may say it can't check the app: open System Settings > Privacy &
   Security, scroll down, click **Open Anyway** and confirm. You only do this once.
3. The first launch downloads the AI model (a few GB, once). The top of the window shows progress.

**Windows**
1. Run `BotTrack-Setup.exe`. If Windows SmartScreen warns you, click **More info**, then **Run anyway**.
2. It installs for your account only (no admin needed) and opens BotTrack. The first launch downloads the AI model.

**Android**
1. Open the `.apk` on your phone and allow installing from your browser or files app when asked.
2. In BotTrack on your computer open **Setup > Phone** and scan the QR code with the app.

**iPhone**
1. In BotTrack on your computer open **Setup > Phone** and scan the QR code with the Camera app.
2. In Safari, tap Share > **Add to Home Screen**.

## Your key

BotTrack needs a key made for your Gmail address. On the key screen press **Request access**: once it's
allowed, the key arrives in that Gmail and BotTrack unlocks by itself. A key only works on a computer where
its Gmail is connected.

## Getting started

On **Setup**, add each Gmail inbox with an app password (https://myaccount.google.com/apppasswords, 2-Step
Verification has to be on) and optionally connect Discord. BotTrack then checks your inboxes (Spam too) every
few hours while it's open. Tap any price or status that looks wrong to fix it: the AI learns from it.

## Updates, backups, space

- **Updates:** BotTrack shows **Update now** when a new build is out (Setup > Updates also has Undo last update).
- **Backups:** Setup > Backups saves a locked copy of everything every day to Documents/BotTrack Backups.
  Restore it there on the same or a new computer.
- **Mac and PC together:** Setup > Your other computers keeps both the same.
- **Space:** Setup > Storage shows what BotTrack takes and frees what it doesn't need.

## The manual

Every release has **BotTrack-Manual.html**: the full manual with pictures, the same one that's on the Manual
page inside BotTrack. Download it and open it in any browser.

## Where your data lives

- Mac: `~/Library/Application Support/BotTrack`
- Windows: `%LOCALAPPDATA%\BotTrack`

To remove BotTrack, delete the app (Windows: Settings > Apps) and, to remove your data and the AI model too,
that folder.
