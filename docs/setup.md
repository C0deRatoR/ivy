# Setup guide

How to turn an old PC into the home server Ivy runs on. Written for the hardware this was built on (Intel i3-3210, 4 GB RAM, generic H61 board, 1 TB HDD), but any x86 PC that runs Debian works.

| Service | What it does | Where it runs | Address |
|---|---|---|---|
| Tailscale | Private access from anywhere, no router ports opened | host | `homeserver` on your tailnet |
| AdGuard Home | Network-wide ad and tracker blocking (DNS) | Docker | `http://homeserver` (setup wizard on `:3000`) |
| Samba | Shared folders on the data disk | host | `smb://homeserver/share` |
| Jellyfin | Media server for movies and shows | Docker | `http://homeserver:8096` |
| Home Assistant | Smart-home hub | Docker | `http://homeserver:8123` |
| Ivy | AI chat assistant that controls all of the above | Docker | Telegram / Discord / Bluetooth speaker |

## 0. Hardware prep

- **Discrete GPU:** while a card like a GT 710 is plugged in, many older BIOSes switch off the CPU's built-in graphics, which is what Jellyfin needs for fast video conversion. Remove the card once the server no longer needs a screen.
- **Check the disk first:** `sudo smartctl -A /dev/sda`. Reallocated or pending sectors above 0 mean the drive is wearing out. (The original WD10EZEX in this build failed its long self-test with 1372 reallocated sectors, so an SSD is the planned replacement.)
- **BIOS** (a generic H61 board with an AMI BIOS, entered with `Del`):
  - **Easy Control → PCIE Wake Control = Enabled** turns on Wake on LAN.
  - This board has **no "restore on AC power loss"** and no wake-on-keyboard. As a fallback, **Easy Control → Wake system with Fixed Time** powers the PC on daily if it's off.
  - Windows can't reboot into this BIOS (`shutdown /fw` isn't supported); press `Del` at power-on.
- In your router's DHCP settings (on a TP-Link Archer C50: Advanced → Network → DHCP Server → Address Reservation), give the server a **DHCP reservation** (fixed LAN IP).

## 1. Install Debian 13

Write the `netinst` ISO from debian.org to a USB stick, check it against `SHA512SUMS`, and boot the **UEFI: <stick>** entry. Installer answers:

- Hostname `homeserver`, domain empty. **Root password empty**, so the first user gets `sudo`.
- Partitioning: **Guided – use entire disk** (the big disk, not the stick), **all files in one partition**.
- Software: **SSH server** and **standard system utilities**. Add **Xfce** only if you want to use the PC as a desktop sometimes.
- **Force GRUB installation to the EFI removable media path: Yes.** Old firmware can lose boot entries otherwise.

After the first boot, everything is done over SSH from your laptop:

```sh
ssh-copy-id <user>@<server-ip>                        # from the laptop, once
sudo apt install -y curl git ethtool rsync
sudo systemctl set-default multi-user.target          # boot to text; desktop on demand: sudo systemctl start lightdm
sudo nmcli connection modify "Wired connection 1" 802-3-ethernet.wake-on-lan magic connection.id lan
sudo nmcli connection up lan                          # Wake on LAN stays armed across reboots

# SSH keys only (the local console still takes the password)
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' |
  sudo tee /etc/ssh/sshd_config.d/10-keys-only.conf
sudo sshd -t && sudo systemctl reload ssh
```

## 2. Tailscale

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

Open the login link it prints. In the Tailscale admin console, open the machine's menu and choose **Disable key expiry** so the server never drops off your tailnet.

Install Tailscale on your laptop, phone and Android TV box (Play Store) with the same account.

`ssh <user>@homeserver` goes through **Tailscale SSH**, which asks for a browser approval each time. Plain SSH to the LAN IP uses your key directly.

## 3. Docker

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in afterwards

# Cap container logs so they can't fill the disk
printf '{\n  "log-driver": "local",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}\n' |
  sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

## Maintenance basics

```sh
sudo apt install -y unattended-upgrades smartmontools   # smartd starts watching the disk
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' |
  sudo tee /etc/apt/apt.conf.d/20auto-upgrades           # security updates, daily
```

## 4. Samba file share

```sh
sudo apt install -y samba
sudo mkdir -p /srv/share/media/movies /srv/share/media/shows /srv/share/files
sudo chown -R $USER:$USER /srv/share

sudo tee -a /etc/samba/smb.conf >/dev/null <<EOF

[share]
   path = /srv/share
   valid users = $USER
   read only = no
EOF

sudo smbpasswd -a $USER   # the password you'll type when connecting
sudo systemctl restart smbd
```

Connect from Windows with `\\homeserver\share`, from Linux and macOS with `smb://homeserver/share`, and from a phone with any SMB file manager. Files you put in `media/movies` and `media/shows` show up in Jellyfin.

## 5. Start the containers

Clone this repository onto the server as `~/homeserver`, then run:

```sh
cd ~/homeserver
sudo ss -lunp | grep ':53 '   # must print nothing, or AdGuard can't take DNS
mkdir -p data/jellyfin/config data/jellyfin/cache   # Jellyfin runs as uid 1000; Docker would create these as root
cp assistant/.env.example assistant/.env && chmod 600 assistant/.env
docker compose up -d adguard jellyfin homeassistant
docker compose ps
```

Time zone defaults to `Asia/Kolkata`. To change it, put `TZ=Your/Zone` in a `.env` file next to `docker-compose.yml`, and set `TZ` in `assistant/.env` too.

## 6. Configure

**AdGuard Home**
1. Open `http://<server-ip>:3000` and complete the wizard. Web UI on all interfaces, port 80; DNS on all interfaces, port 53.
2. Add a stronger block list next to the default AdGuard DNS filter: Filters → DNS blocklists → Add → custom list
   `https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/pro.txt` (HaGeZi Pro).
3. Tailscale admin console → **DNS** → Add nameserver → Custom → the server's Tailscale IP (`100.x.y.z`; no Split DNS, no exit node) → turn on **Override DNS servers**. Every device on the tailnet then gets ad blocking anywhere, including a phone on mobile data and an Android TV box running the Tailscale app.
4. On Android, set **Settings → Private DNS** to Off or Automatic, otherwise Android bypasses it.
5. Leave the router's own DNS (whole house, devices without Tailscale) alone until the server is reliable (SSD and UPS): when the server is down, every device using it can't look up any websites.

If a server problem breaks browsing on a device, disconnect Tailscale on that device. If an app breaks on the TV box, find the blocked domain in AdGuard's Query Log and unblock it.

**Jellyfin.** Open `http://<server-ip>:8096`, create the admin user (keep **automatic port mapping off**), then add libraries pointing at `/media/movies` and `/media/shows`. Under Dashboard → Networking → LAN networks, add your LAN subnet (for example `<lan-subnet>/24`) and `100.64.0.0/10`, so Tailscale devices count as local and aren't throttled. On an Android TV box, use the **Jellyfin for Android TV** app with server `http://homeserver:8096`.

**Home Assistant.** Open `http://<server-ip>:8123` and complete onboarding. Integrations used here:
- **Android TV Remote:** the TV box (pairing code shown on the TV).
- **WiZ:** Philips WiZ bulbs. First turn on **Allow local communication** in the WiZ app (Settings → Security).
- Skipped: the router's UPnP/IGD (UPnP should be off on the router anyway).

## 7. Ivy, the AI assistant

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and/or a Discord bot at <https://discord.com/developers/applications>. For Discord, invite the bot to a server you're in, then DM it or @mention it.
2. Get an LLM key. For Gemini's free tier, use <https://aistudio.google.com/apikey>. Any OpenAI-compatible API works; see `assistant/.env.example`.
3. Fill in `assistant/.env`. Leave the allowed user IDs empty at first if you don't know them, then message the bot: it replies with your ID. Add it and restart.
4. Create the service credentials Ivy uses: a Home Assistant long-lived access token, a Jellyfin API key, and the AdGuard admin login.
5. `docker compose up -d --build assistant`, then `docker compose logs -f assistant`.

Development on another machine (no Docker needed):

```sh
cd assistant
uv run pytest
uv run --env-file .env python -m homebot
```

## 8. Ivy speaker (voice)

Turns a Bluetooth speaker with a microphone into an Alexa-style speaker: say "Ivy, turn on the bedroom light" and she answers out loud. The wake word, speech to text (Whisper) and Ivy's voice (Piper) all run on the server; room audio never leaves it. Only the text of a request, after the wake word, goes to the LLM, the same as a Telegram message.

Things to know first:
- The speaker has to stay in Bluetooth headset mode (HSP/HFP) to use its mic, which makes it mono and call quality. Fine for Ivy's voice, poor for music.
- Anyone in the room can talk to Ivy. Risky actions still need a spoken "yes".
- The H61 board has no Bluetooth. A USB adapter with a **Realtek RTL8761B** chip ("Bluetooth 5.0 USB adapter") works with Debian's `firmware-realtek`.

**Host: Bluetooth and PipeWire.** The container plays and records through the host's PipeWire, which needs to run without anyone logged in:

```sh
sudo apt install firmware-realtek bluez pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth pulseaudio-utils
sudo loginctl enable-linger $USER   # start your user services (PipeWire) at boot, without a login
sudo reboot
```

**Pair the speaker.** Put it in pairing mode, then:

```sh
bluetoothctl
  scan on              # wait for the speaker to appear, note its MAC
  pair <MAC>
  trust <MAC>          # so it reconnects by itself after a power cut
  connect <MAC>
  quit
pactl list cards | grep -A8 'Profiles:'      # needs a headset-head-unit profile
pactl set-card-profile bluez_card.<MAC with _ instead of :> headset-head-unit
pactl list short sources; pactl list short sinks   # note the bluez_input... and bluez_output... names
```

**Turn it on.** In `assistant/.env` set `VOICE_WAKE_WORD=hey_jarvis` (a ready-made stand-in until the Ivy model exists), and `VOICE_SOURCE` / `VOICE_SINK` to the `bluez_input...` and `bluez_output...` names. In `docker-compose.yml`, uncomment the PipeWire socket line and `PULSE_SERVER` under `assistant`. Then:

```sh
docker compose up -d --build assistant
docker compose logs -f assistant   # the first start downloads about 220 MB of models into the assistant-data volume
```

Say "Hey Jarvis, what time is it?". If you say only the wake word, she chimes and listens for the request. After she asks "Should I do this?", she chimes again for your "yes".

**Train the "Ivy" wake word (one time, about an hour, free).** "Ivy" has no ready-made model, so train one with openWakeWord's Colab notebook. It generates thousands of synthetic "Ivy" clips and needs no recordings of you.

1. Open the [openWakeWord training notebook](https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb?usp=sharing) while signed in to Google, then **File → Save a copy in Drive**.
2. **Runtime → Change runtime type → T4 GPU.**
3. Set the target word to `ivy`. Play the pronunciation sample the notebook makes; if it sounds wrong, try `eye vee`. Keep the other settings at their defaults.
4. **Runtime → Run all**, and wait (about an hour). Download the `.onnx` file it produces and rename it `ivy.onnx`.
5. Copy it to the server and switch over:

   ```sh
   scp ivy.onnx <user>@<server-ip>:~/homeserver/
   docker compose cp ivy.onnx assistant:/data/voice/ivy.onnx
   ```

   Set `VOICE_WAKE_WORD=ivy` in `assistant/.env`, then `docker compose up -d assistant`.

A short word like "Ivy" wakes by mistake more often than a longer phrase, and a Bluetooth headset mic is telephone quality (nothing above 4 kHz), which makes it worse. Whisper can't reliably hear the wake word itself through that mic, so Ivy trusts the wake word detector and treats whatever is said right after it as the request. A false wake therefore gets a reply now and then. If she answers when she shouldn't, or misses you, change `WAKE_THRESHOLD` in `assistant/homebot/voice.py` (default 0.5; higher means stricter). A USB microphone on the server gives full-quality audio and fewer mistakes, and lets the speaker stay in music-quality mode.

## Updating

Container versions are pinned in `docker-compose.yml`. To update a service, change its tag to the new version, then:

```sh
docker compose pull && docker compose up -d
```

Debian security updates install themselves daily. For everything else: `sudo apt update && sudo apt upgrade`.

## Backup

From the laptop, copy the service settings (not the media) off the server:

```sh
ssh <user>@<server-ip> 'sudo tar -czf - -C / --exclude=home/<user>/homeserver/data/jellyfin/cache \
  home/<user>/homeserver etc/samba/smb.conf etc/docker/daemon.json etc/NetworkManager/system-connections' \
  > ~/Backups/homeserver/homeserver-$(date +%F).tar.gz
```
