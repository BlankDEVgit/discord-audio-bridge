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

# Tuning — safe defaults, override in .env if needed
AUDIO_BUFFER_SIZE = int(os.getenv("AUDIO_BUFFER_SIZE", "10"))        # DirectShow buffer (ms)
PROBE_SIZE = int(os.getenv("PROBE_SIZE", "32"))                      # FFmpeg probe size
THREAD_QUEUE_SIZE = int(os.getenv("THREAD_QUEUE_SIZE", "32"))        # Read thread queue
RT_BUFFER_SIZE = os.getenv("RT_BUFFER_SIZE", "64k")                  # Real-time buffer
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "2"))             # Seconds before retry
MAX_RECONNECT_DELAY = int(os.getenv("MAX_RECONNECT_DELAY", "30"))    # Max backoff cap
HEALTH_CHECK_INTERVAL = float(os.getenv("HEALTH_CHECK_INTERVAL", "0.5"))  # Stream poll (s)
PROCESS_PRIORITY = os.getenv("PROCESS_PRIORITY", "realtime")          # "realtime", "high", or "normal"
SELF_DEAF = os.getenv("SELF_DEAF", "true").lower() == "true"         # Deaf the bot in VC
ENABLE_CONSOLE = os.getenv("ENABLE_CONSOLE", "true").lower() == "true"  # Command console on/off

# ──────────────────────────────────────────────
#  Runtime state — mutable, changed via commands
# ──────────────────────────────────────────────
runtime = {
    "quality": os.getenv("AUDIO_QUALITY", "balanced"),
    "bad_internet": os.getenv("BAD_INTERNET", "false").lower() == "true",
    "vc": None,
}

# ──────────────────────────────────────────────
#  Audio quality presets
#  Bitrate and channel count — no latency impact
#  (Opus always encodes in fixed 20ms frames)
# ──────────────────────────────────────────────
QUALITY_PRESETS = {
    "low":      {"bitrate": 48_000,  "channels": 1},
    "balanced": {"bitrate": 128_000, "channels": 2},  # matches discord.py default
    "high":     {"bitrate": 256_000, "channels": 2},
    "ultra":    {"bitrate": 384_000, "channels": 2},
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
    if runtime["quality"] not in QUALITY_PRESETS:
        errors.append(f"AUDIO_QUALITY '{runtime['quality']}' is invalid — use: {', '.join(QUALITY_PRESETS)}.")
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
#  Audio source
# ──────────────────────────────────────────────
def create_audio_source():
    """
    Build an FFmpeg audio source tuned for ultra-low latency.

    This is your original pipeline — hardcoded for maximum performance.
    Only overridden when a non-default preset or bad-internet is active.
    """
    # Default: exact original pipeline — no indirection, no overhead
    if runtime["quality"] == "balanced" and not runtime["bad_internet"]:
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

    # Non-default preset: use configurable values
    channels = BAD_INTERNET_OVERRIDES["channels"] if runtime["bad_internet"] \
        else QUALITY_PRESETS[runtime["quality"]]["channels"]

    return discord.FFmpegPCMAudio(
        f"audio={AUDIO_DEVICE}",
        executable=FFMPEG_PATH,
        before_options=(
            f'-f dshow '
            f'-audio_buffer_size {AUDIO_BUFFER_SIZE} '
            f'-probesize {PROBE_SIZE} -analyzeduration 0 '
            '-fflags nobuffer+discardcorrupt+flush_packets '
            '-flags low_delay '
            '-avioflags direct '
            f'-thread_queue_size {THREAD_QUEUE_SIZE} '
            f'-rtbufsize {RT_BUFFER_SIZE}'
        ),
        options=(
            f'-ac {channels} -ar 48000 -f s16le '
            '-flush_packets 1 '
            '-fflags +flush_packets'
        ),
    )


# ──────────────────────────────────────────────
#  Opus encoder configuration
# ──────────────────────────────────────────────
def configure_encoder(vc):
    """Apply quality/bad-internet settings to the Opus encoder.

    On 'balanced' without bad-internet, does nothing — discord.py's
    native defaults (128kbps stereo) are already optimal.
    """
    if runtime["quality"] == "balanced" and not runtime["bad_internet"]:
        return

    if runtime["bad_internet"]:
        bitrate = BAD_INTERNET_OVERRIDES["bitrate"]
        fec = True
        plp = BAD_INTERNET_OVERRIDES["packet_loss_percent"]
        label = "bad internet"
    else:
        preset = QUALITY_PRESETS[runtime["quality"]]
        bitrate = preset["bitrate"]
        fec = False
        plp = 0
        label = runtime["quality"]

    try:
        vc.encoder.set_bitrate(bitrate)
        vc.encoder.set_fec(fec)
        vc.encoder.set_expected_packet_loss_percent(plp)
        ch = "mono" if (runtime["bad_internet"] or QUALITY_PRESETS[runtime["quality"]]["channels"] == 1) else "stereo"
        print(f"[QUALITY] {label} — {bitrate // 1000}kbps {ch}{' + FEC' if fec else ''}")
    except Exception as exc:
        print(f"[QUALITY] Could not configure encoder: {exc}")


# ──────────────────────────────────────────────
#  Discord client setup
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
#  Voice streaming loop
# ──────────────────────────────────────────────
async def stream_audio():
    """Persistent voice-channel loop with automatic reconnection."""
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

            runtime["vc"] = vc
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

            runtime["vc"] = None
            print("[STREAM] Disconnected — reconnecting...")

        except Exception as exc:
            print(f"[STREAM] Error ({type(exc).__name__}): {exc}")
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)

        await asyncio.sleep(backoff)


# ──────────────────────────────────────────────
#  Live settings swap (used by command console)
# ──────────────────────────────────────────────
def apply_live_settings():
    """Apply current runtime settings to the active voice client."""
    vc = runtime["vc"]
    if vc is None or not vc.is_connected():
        print("[CMD] Not connected — settings will apply on next connect.")
        return

    # If channel count changed (mono <-> stereo), restart FFmpeg source
    old_channels = runtime.get("_last_channels", 2)
    if runtime["bad_internet"]:
        new_channels = BAD_INTERNET_OVERRIDES["channels"]
    else:
        new_channels = QUALITY_PRESETS[runtime["quality"]]["channels"]

    if new_channels != old_channels and vc.is_playing():
        print("[CMD] Channel count changed — restarting audio source...")
        vc.stop()
        vc.play(create_audio_source())

    runtime["_last_channels"] = new_channels

    # Force encoder config even on balanced (user explicitly asked)
    if runtime["bad_internet"]:
        bitrate = BAD_INTERNET_OVERRIDES["bitrate"]
        fec = True
        plp = BAD_INTERNET_OVERRIDES["packet_loss_percent"]
        label = "bad internet"
    else:
        preset = QUALITY_PRESETS[runtime["quality"]]
        bitrate = preset["bitrate"]
        fec = False
        plp = 0
        label = runtime["quality"]

    try:
        vc.encoder.set_bitrate(bitrate)
        vc.encoder.set_fec(fec)
        vc.encoder.set_expected_packet_loss_percent(plp)
        ch = "mono" if new_channels == 1 else "stereo"
        print(f"[QUALITY] {label} — {bitrate // 1000}kbps {ch}{' + FEC' if fec else ''}")
    except Exception as exc:
        print(f"[QUALITY] Could not apply: {exc}")


# ──────────────────────────────────────────────
#  Command console (optional, runs alongside bot)
# ──────────────────────────────────────────────
HELP_TEXT = """
Commands (type while the bot is running):
  quality <preset>       Set audio quality: low, balanced, high, ultra
  bad-internet <on|off>  Toggle bad internet mode (FEC + low bitrate)
  status                 Show current settings
  help                   Show this message
  quit / exit            Stop the bot
""".strip()


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
                    br = BAD_INTERNET_OVERRIDES["bitrate"]
                    ch = "mono"
                    fec = True
                else:
                    preset = QUALITY_PRESETS[runtime["quality"]]
                    br = preset["bitrate"]
                    ch = "mono" if preset["channels"] == 1 else "stereo"
                    fec = False
                vc = runtime["vc"]
                connected = "yes" if vc and vc.is_connected() else "no"
                playing = "yes" if vc and vc.is_playing() else "no"
                print(f"[STATUS] Quality: {runtime['quality']} | "
                      f"Bad internet: {'on' if runtime['bad_internet'] else 'off'} | "
                      f"{br // 1000}kbps {ch}{' + FEC' if fec else ''} | "
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


@client.event
async def on_ready():
    """Start the audio stream once the bot is connected to Discord."""
    global voice_task
    print(f"[BOT] Logged in as {client.user}")
    if voice_task is None or voice_task.done():
        voice_task = asyncio.create_task(stream_audio())
        if ENABLE_CONSOLE:
            asyncio.create_task(command_console())


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
