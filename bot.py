"""
Discord Audio Bridge
Created by Blank

Captures system audio via a virtual audio cable and streams it
to a Discord voice channel with ultra-low latency.

Use case: bridge PC audio to Discord so you can hear your PC
through any device connected to the same voice channel (e.g. PS5).
"""

import asyncio
import functools
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
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "2"))
ENABLE_CONSOLE = os.getenv("ENABLE_CONSOLE", "true").lower() == "true"

# ──────────────────────────────────────────────
#  CPU priority — set immediately like the original
# ──────────────────────────────────────────────
try:
    p = psutil.Process(os.getpid())
    p.nice(psutil.REALTIME_PRIORITY_CLASS)
    print("[PRIORITY] Realtime — lowest possible latency.")
except Exception:
    try:
        p = psutil.Process(os.getpid())
        p.nice(psutil.HIGH_PRIORITY_CLASS)
        print("[PRIORITY] High — reduced latency jitter.")
    except Exception as e:
        print(f"[PRIORITY] Could not elevate: {e}")

# ──────────────────────────────────────────────
#  Audio quality presets (Opus encoder only)
#
#  IMPORTANT: These ONLY change the Opus encoder
#  bitrate and FEC. The FFmpeg pipeline is NEVER
#  touched — it always outputs stereo 48kHz PCM.
#  discord.py requires -ac 2 at all times.
# ──────────────────────────────────────────────
QUALITY_PRESETS = {
    "low":      {"bitrate": 48_000,  "fec": False},
    "balanced": {"bitrate": 128_000, "fec": False},   # discord.py default — encoder untouched
    "high":     {"bitrate": 256_000, "fec": False},
    "ultra":    {"bitrate": 384_000, "fec": False},
}

BAD_INTERNET = {
    "bitrate": 32_000,
    "fec": True,
    "packet_loss_percent": 25,
}

# Runtime state
runtime = {
    "quality": os.getenv("AUDIO_QUALITY", "balanced"),
    "bad_internet": os.getenv("BAD_INTERNET", "false").lower() == "true",
    "vc": None,
}

# ──────────────────────────────────────────────
#  Discord client — minimal intents
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = False
intents.members = False
intents.presences = False
intents.typing = False

client = discord.Client(intents=intents)
voice_task = None


# ──────────────────────────────────────────────
#  Audio source — identical to original
# ──────────────────────────────────────────────
def create_audio_source():
    """Ultra low-latency FFmpeg capture from virtual cable."""
    return discord.FFmpegPCMAudio(
        f"audio={AUDIO_DEVICE}",
        executable=FFMPEG_PATH,
        before_options=(
            '-f dshow '
            '-audio_buffer_size 10 '
            '-probesize 32 -analyzeduration 0 '
            '-fflags nobuffer+discardcorrupt+flush_packets '
            '-flags low_delay '
            '-avioflags direct '
            '-thread_queue_size 32 '
            '-rtbufsize 64k'
        ),
        options=(
            '-ac 2 -ar 48000 -f s16le '
            '-flush_packets 1 '
            '-fflags +flush_packets'
        ),
    )


# ──────────────────────────────────────────────
#  Opus encoder configuration
# ──────────────────────────────────────────────
def configure_encoder(vc):
    """Apply quality preset to the Opus encoder only.

    On 'balanced' without bad-internet, does nothing — the encoder
    keeps discord.py's native 128kbps defaults untouched.
    """
    if runtime["quality"] == "balanced" and not runtime["bad_internet"]:
        return

    try:
        if runtime["bad_internet"]:
            vc.encoder.set_bitrate(BAD_INTERNET["bitrate"])
            vc.encoder.set_fec(True)
            vc.encoder.set_expected_packet_loss_percent(BAD_INTERNET["packet_loss_percent"])
            print(f"[QUALITY] bad internet — {BAD_INTERNET['bitrate'] // 1000}kbps + FEC")
        else:
            preset = QUALITY_PRESETS[runtime["quality"]]
            vc.encoder.set_bitrate(preset["bitrate"])
            vc.encoder.set_fec(preset["fec"])
            vc.encoder.set_expected_packet_loss_percent(0)
            print(f"[QUALITY] {runtime['quality']} — {preset['bitrate'] // 1000}kbps")
    except Exception as exc:
        print(f"[QUALITY] Could not configure encoder: {exc}")


# ──────────────────────────────────────────────
#  Voice streaming loop
# ──────────────────────────────────────────────
async def stream_audio():
    """Persistent voice-channel loop with auto-reconnect."""
    if not os.path.isfile(FFMPEG_PATH):
        print(f"[ERROR] ffmpeg not found at '{FFMPEG_PATH}'!")
        return

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

            # Clean up stale voice client
            if guild.voice_client is not None:
                try:
                    guild.voice_client.stop()
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            print(f"[STREAM] Connecting to #{channel.name} ({guild.name})...")
            vc = await channel.connect(timeout=15.0, reconnect=True, self_deaf=True)
            print("[STREAM] Audio streaming started.")
            backoff = RECONNECT_DELAY

            runtime["vc"] = vc
            vc.play(create_audio_source())
            configure_encoder(vc)

            # Health monitor — fast poll for quick recovery
            while vc.is_connected():
                if not vc.is_playing():
                    print("[STREAM] Source stopped — restarting...")
                    try:
                        vc.play(create_audio_source())
                        configure_encoder(vc)
                    except Exception as exc:
                        print(f"[STREAM] Restart failed: {exc}")
                        break
                await asyncio.sleep(0.5)

            runtime["vc"] = None
            print("[STREAM] Disconnected — reconnecting...")

        except Exception as exc:
            print(f"[STREAM] Error ({type(exc).__name__}): {exc}")
            backoff = min(backoff * 2, 30)

        await asyncio.sleep(backoff)


# ──────────────────────────────────────────────
#  Command console
# ──────────────────────────────────────────────
HELP_TEXT = """
Commands (type while the bot is running):
  quality <preset>       Set audio quality: low, balanced, high, ultra
  bad-internet <on|off>  Toggle bad internet mode (FEC + low bitrate)
  status                 Show current settings
  help                   Show this message
  quit / exit            Stop the bot
""".strip()


def apply_live_settings():
    """Apply current runtime settings to the active Opus encoder."""
    vc = runtime["vc"]
    if vc is None or not vc.is_connected():
        print("[CMD] Not connected — settings will apply on next connect.")
        return

    try:
        if runtime["bad_internet"]:
            vc.encoder.set_bitrate(BAD_INTERNET["bitrate"])
            vc.encoder.set_fec(True)
            vc.encoder.set_expected_packet_loss_percent(BAD_INTERNET["packet_loss_percent"])
            print(f"[QUALITY] bad internet — {BAD_INTERNET['bitrate'] // 1000}kbps + FEC")
        else:
            preset = QUALITY_PRESETS[runtime["quality"]]
            vc.encoder.set_bitrate(preset["bitrate"])
            vc.encoder.set_fec(preset["fec"])
            vc.encoder.set_expected_packet_loss_percent(0)
            print(f"[QUALITY] {runtime['quality']} — {preset['bitrate'] // 1000}kbps")
    except Exception as exc:
        print(f"[QUALITY] Could not apply: {exc}")


async def command_console():
    """Read commands from stdin without blocking the event loop."""
    await client.wait_until_ready()
    loop = asyncio.get_event_loop()
    print(f"\n{HELP_TEXT}\n")

    while True:
        try:
            line = await loop.run_in_executor(None, functools.partial(sys.stdin.readline))
            cmd = line.strip().lower()
            if not cmd:
                continue

            parts = cmd.split(None, 1)
            command = parts[0]
            arg = parts[1] if len(parts) > 1 else ""

            if command == "help":
                print(HELP_TEXT)

            elif command == "quality":
                if arg not in QUALITY_PRESETS:
                    print(f"[CMD] Invalid preset. Choose: {', '.join(QUALITY_PRESETS)}")
                else:
                    runtime["quality"] = arg
                    apply_live_settings()

            elif command == "bad-internet":
                if arg in ("on", "true", "1"):
                    runtime["bad_internet"] = True
                    apply_live_settings()
                elif arg in ("off", "false", "0"):
                    runtime["bad_internet"] = False
                    apply_live_settings()
                else:
                    print("[CMD] Usage: bad-internet <on|off>")

            elif command == "status":
                if runtime["bad_internet"]:
                    br = BAD_INTERNET["bitrate"] // 1000
                    extra = " + FEC"
                else:
                    br = QUALITY_PRESETS[runtime["quality"]]["bitrate"] // 1000
                    extra = ""
                vc = runtime["vc"]
                connected = "yes" if vc and vc.is_connected() else "no"
                playing = "yes" if vc and vc.is_playing() else "no"
                print(f"[STATUS] Quality: {runtime['quality']} | "
                      f"Bad internet: {'on' if runtime['bad_internet'] else 'off'} | "
                      f"{br}kbps{extra} | "
                      f"Connected: {connected} | Playing: {playing}")

            elif command in ("quit", "exit"):
                print("[BOT] Shutting down...")
                await client.close()
                return

            else:
                print(f"[CMD] Unknown command: {command} — type 'help' for options.")

        except (EOFError, KeyboardInterrupt):
            return
        except Exception as exc:
            print(f"[CMD] Error: {exc}")


# ──────────────────────────────────────────────
#  Bot startup
# ──────────────────────────────────────────────
@client.event
async def on_ready():
    global voice_task
    print(f"[BOT] Logged in as {client.user}")
    if voice_task is None or voice_task.done():
        voice_task = asyncio.create_task(stream_audio())
        if ENABLE_CONSOLE:
            asyncio.create_task(command_console())


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("[ERROR] BOT_TOKEN is missing — set it in your .env file.")
        sys.exit(1)
    if VOICE_CHANNEL_ID == 0:
        print("[ERROR] VOICE_CHANNEL_ID is missing — set it in your .env file.")
        sys.exit(1)
    try:
        client.run(BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        print("\n[BOT] Stopped.")
