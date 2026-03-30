"""
Audio Retransmission Bot
Created by Blank

Captures system audio via a virtual audio cable and streams it
to a Discord voice channel with ultra-low latency.

Use case: bridge PC audio to Discord so you can hear your PC
through any device connected to the same voice channel (e.g. PS5).
"""

import asyncio
import os
import sys

import discord
import psutil
from dotenv import load_dotenv

# ──────────────────────────────────────────────
#  Configuration — loaded from .env file
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "CABLE Output (VB-Audio Virtual Cable)")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "0"))
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg.exe")

# Tuning — safe defaults, override in .env if needed
AUDIO_BUFFER_SIZE = int(os.getenv("AUDIO_BUFFER_SIZE", "10"))        # DirectShow buffer (ms)
PROBE_SIZE = int(os.getenv("PROBE_SIZE", "32"))                      # FFmpeg probe size
THREAD_QUEUE_SIZE = int(os.getenv("THREAD_QUEUE_SIZE", "32"))        # Read thread queue
RT_BUFFER_SIZE = os.getenv("RT_BUFFER_SIZE", "64k")                  # Real-time buffer
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "2"))             # Seconds before retry
MAX_RECONNECT_DELAY = int(os.getenv("MAX_RECONNECT_DELAY", "30"))    # Max backoff cap
HEALTH_CHECK_INTERVAL = float(os.getenv("HEALTH_CHECK_INTERVAL", "0.5"))  # Stream poll (s)
PROCESS_PRIORITY = os.getenv("PROCESS_PRIORITY", "high")             # "realtime", "high", or "normal"
SELF_DEAF = os.getenv("SELF_DEAF", "true").lower() == "true"         # Deaf the bot in VC
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "balanced")               # "low", "balanced", "high", "ultra"
BAD_INTERNET = os.getenv("BAD_INTERNET", "false").lower() == "true"  # Enable FEC + reduced bitrate

# ──────────────────────────────────────────────
#  Audio quality presets
#  Bitrate and channel count — no latency impact
#  (Opus always encodes in fixed 20ms frames)
# ──────────────────────────────────────────────
QUALITY_PRESETS = {
    "low":      {"bitrate": 32_000,  "channels": 1},
    "balanced": {"bitrate": 64_000,  "channels": 2},
    "high":     {"bitrate": 96_000,  "channels": 2},
    "ultra":    {"bitrate": 128_000, "channels": 2},
}

BAD_INTERNET_OVERRIDES = {
    "bitrate": 32_000,
    "channels": 1,
    "fec": True,
    "packet_loss_percent": 25,
}


# ──────────────────────────────────────────────
#  Startup validation
# ──────────────────────────────────────────────
def validate_config():
    """Check that all required settings are present before running."""
    errors = []
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is missing — set it in your .env file.")
    if VOICE_CHANNEL_ID == 0:
        errors.append("VOICE_CHANNEL_ID is missing — set it in your .env file.")
    if not os.path.isfile(FFMPEG_PATH):
        errors.append(f"FFmpeg not found at '{FFMPEG_PATH}' — download it or set FFMPEG_PATH.")
    if AUDIO_QUALITY not in QUALITY_PRESETS:
        errors.append(f"AUDIO_QUALITY '{AUDIO_QUALITY}' is invalid — use: {', '.join(QUALITY_PRESETS)}.")
    if errors:
        for err in errors:
            print(f"[ERROR] {err}")
        sys.exit(1)


# ──────────────────────────────────────────────
#  Process priority boost
# ──────────────────────────────────────────────
def set_process_priority():
    """Raise process priority to reduce audio latency jitter."""
    if PROCESS_PRIORITY == "normal":
        return

    proc = psutil.Process(os.getpid())

    if PROCESS_PRIORITY == "realtime":
        try:
            proc.nice(psutil.REALTIME_PRIORITY_CLASS)
            print("[PRIORITY] Realtime — lowest possible latency.")
            return
        except Exception:
            print("[PRIORITY] Realtime denied, falling back to High.")

    try:
        proc.nice(psutil.HIGH_PRIORITY_CLASS)
        print("[PRIORITY] High — reduced latency jitter.")
    except Exception as exc:
        print(f"[PRIORITY] Could not elevate priority: {exc}")


# ──────────────────────────────────────────────
#  Audio settings resolver
# ──────────────────────────────────────────────
def get_audio_settings():
    """Resolve the active audio settings from preset + bad internet mode."""
    preset = QUALITY_PRESETS[AUDIO_QUALITY]
    if BAD_INTERNET:
        return {
            "bitrate": BAD_INTERNET_OVERRIDES["bitrate"],
            "channels": BAD_INTERNET_OVERRIDES["channels"],
            "fec": True,
            "packet_loss_percent": BAD_INTERNET_OVERRIDES["packet_loss_percent"],
        }
    return {
        "bitrate": preset["bitrate"],
        "channels": preset["channels"],
        "fec": False,
        "packet_loss_percent": 0,
    }


# ──────────────────────────────────────────────
#  Audio source
# ──────────────────────────────────────────────
def create_audio_source():
    """
    Build an FFmpeg audio source tuned for ultra-low latency.

    The pipeline: DirectShow capture → raw PCM at 48 kHz →
    Discord voice connection. Every buffer and probe setting is
    minimized to keep end-to-end delay as short as possible.
    """
    settings = get_audio_settings()
    channels = settings["channels"]

    return discord.FFmpegPCMAudio(
        f"audio={AUDIO_DEVICE}",
        executable=FFMPEG_PATH,
        before_options=(
            # DirectShow capture with minimal buffer
            f"-f dshow "
            f"-audio_buffer_size {AUDIO_BUFFER_SIZE} "
            # Skip probing — the format is known
            f"-probesize {PROBE_SIZE} -analyzeduration 0 "
            # Aggressive low-latency flags
            "-fflags nobuffer+discardcorrupt+flush_packets "
            "-flags low_delay "
            "-avioflags direct "
            # Minimal read buffer
            f"-thread_queue_size {THREAD_QUEUE_SIZE} "
            f"-rtbufsize {RT_BUFFER_SIZE}"
        ),
        options=(
            # Output: raw PCM at Discord's native format (48 kHz, 16-bit)
            f"-ac {channels} -ar 48000 -f s16le "
            # Force-flush every packet — zero output buffering
            "-flush_packets 1 "
            "-fflags +flush_packets"
        ),
    )


# ──────────────────────────────────────────────
#  Opus encoder configuration
# ──────────────────────────────────────────────
def configure_encoder(vc):
    """Apply quality preset and bad-internet settings to the Opus encoder."""
    settings = get_audio_settings()
    try:
        vc.encoder.set_bitrate(settings["bitrate"])
        vc.encoder.set_fec(settings["fec"])
        vc.encoder.set_expected_packet_loss_percent(settings["packet_loss_percent"])

        mode = "bad internet" if BAD_INTERNET else AUDIO_QUALITY
        ch_label = "mono" if settings["channels"] == 1 else "stereo"
        print(f"[QUALITY] {mode} — {settings['bitrate'] // 1000}kbps {ch_label}"
              f"{' + FEC' if settings['fec'] else ''}")
    except Exception as exc:
        print(f"[QUALITY] Could not configure encoder: {exc}")


# ──────────────────────────────────────────────
#  Discord client setup
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
# Disable unused heavy intents
intents.message_content = False
intents.members = False
intents.presences = False
intents.typing = False

client = discord.Client(intents=intents)
voice_task = None


# ──────────────────────────────────────────────
#  Voice streaming loop
# ──────────────────────────────────────────────
async def stream_audio():
    """
    Persistent voice-channel loop with automatic reconnection.

    Connects to the configured voice channel, starts streaming,
    and monitors the audio source. If the connection drops or the
    source stops, it reconnects with exponential backoff.
    """
    await client.wait_until_ready()
    backoff = RECONNECT_DELAY

    while True:
        try:
            channel = client.get_channel(VOICE_CHANNEL_ID)
            if channel is None:
                print(f"[STREAM] Channel {VOICE_CHANNEL_ID} not found — retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                continue

            guild = channel.guild

            # Disconnect any stale voice client
            if guild.voice_client is not None:
                try:
                    guild.voice_client.stop()
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            print(f"[STREAM] Connecting to #{channel.name} ({guild.name})...")
            vc = await channel.connect(timeout=15.0, reconnect=True, self_deaf=SELF_DEAF)
            print("[STREAM] Audio streaming started.")
            backoff = RECONNECT_DELAY

            vc.play(create_audio_source())
            configure_encoder(vc)

            # Health monitor — restart source if it stops unexpectedly
            while vc.is_connected():
                if not vc.is_playing():
                    print("[STREAM] Source stopped — restarting...")
                    try:
                        vc.play(create_audio_source())
                        configure_encoder(vc)
                    except Exception as exc:
                        print(f"[STREAM] Restart failed: {exc}")
                        break
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

            print("[STREAM] Disconnected — reconnecting...")

        except Exception as exc:
            print(f"[STREAM] Error ({type(exc).__name__}): {exc}")
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)

        await asyncio.sleep(backoff)


@client.event
async def on_ready():
    """Start the audio stream once the bot is connected to Discord."""
    global voice_task
    print(f"[BOT] Logged in as {client.user}")
    if voice_task is None or voice_task.done():
        voice_task = asyncio.create_task(stream_audio())


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    validate_config()
    set_process_priority()
    try:
        client.run(BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        print("\n[BOT] Stopped.")
