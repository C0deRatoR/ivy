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

Talk to Ivy like Alexa: say "Ivy, turn on the bedroom light" and she answers out loud through a speaker. Speech to text (Whisper) and Ivy's voice (Piper) run on the server, so room audio never leaves it. Whisper transcribes everything said near the mic, but only a sentence that starts with "Ivy" (or "Hey Ivy") goes to the LLM, the same as a Telegram message.

Things to know first:
- **Use a USB microphone.** A Bluetooth speaker's own mic only works in headset mode, which is telephone quality: Whisper still catches "Ivy" through it, but often garbles the request ("pause ad blocking" came out as "one side of looking"). It also makes the speaker mono and call quality. With a USB mic, the Bluetooth speaker stays in music-quality mode and is only used for Ivy's voice.
- Anyone in the room can talk to Ivy. Risky actions still need a spoken "yes".
- Every sentence said near the mic is transcribed, which keeps a CPU core busy while people talk (about 0.5 s of work per sentence on a modern laptop; slower on the i3).
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
pactl list short sources; pactl list short sinks   # note the mic (alsa_input...) and speaker (bluez_output...) names
```

To use the speaker's own mic instead of a USB one, switch it to headset mode first: `pactl set-card-profile bluez_card.<MAC with _ instead of :> headset-head-unit`, and use its `bluez_input...` source.

**Turn it on.** In `assistant/.env` set `VOICE_WAKE_WORD=ivy`, and `VOICE_SOURCE` / `VOICE_SINK` to the mic and speaker names. In `docker-compose.yml`, uncomment the PipeWire socket line and `PULSE_SERVER` under `assistant`. Then:

```sh
docker compose up -d --build assistant
docker compose logs -f assistant   # the first start downloads about 210 MB of models into the assistant-data volume
```

Say "Ivy, what time is it?". If you say only "Ivy", she chimes and listens for the request. After she asks "Should I do this?", she chimes again for your "yes".

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
