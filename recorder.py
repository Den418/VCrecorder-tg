"""
Core call-recording engine.

How it works
------------
Telegram group voice/video chats don't expose one pre-mixed "recording"
stream — pytgcalls delivers a `Frame` per active speaker (keyed by `ssrc`)
through its `stream_frame` callback. To get a clean result each participant
is recorded to their own raw track; tracks are mixed down with ffmpeg once
the recording stops. This is the same approach recording bots for other
platforms (e.g. Craig for Discord) use, and it doubles as a way to offer
separate per-participant tracks as a bonus export.

Video chats are composited into a single gallery-style grid (à la Zoom/Meet
"gallery view") via ffmpeg's `xstack`: each participant's camera is encoded
individually first, then scaled into equal-sized tiles and stacked. A
participant whose clip ends early is frozen on their last frame (`tpad`)
rather than cutting the whole grid short. The soundtrack is every
participant's mixed audio, including anyone who never turned their camera
on — not just the audio of whoever happens to be on camera. Sync is
best-effort: frame timing is derived from capture timestamps assuming a
roughly steady capture rate, which is a documented limitation on an unstable
connection, not a bug to chase.

Consent
-------
`config.ANNOUNCE_RECORDING` makes the userbot post a message in the chat
whenever it starts/stops recording. Recording a call without telling the
people on it can be illegal (wiretapping / data-protection law) depending on
where your users are — keep this on unless you've built your own compliant
consent flow.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg
from pyrogram import Client
from pyrogram.errors import RPCError
from pytgcalls import PyTgCalls
from pytgcalls import filters as pfilters
from pytgcalls.types import AudioQuality, Device, Direction, RecordStream, StreamFrames, UpdatedGroupCallParticipant

import config
from database import crud
from database.models import Recording, RecordingKind, RecordingStatus, StorageBackend
import filestore

log = logging.getLogger(__name__)

# imageio-ffmpeg bundles a full static ffmpeg build (verified to include
# libx264, libopus, aac, and the xstack/tpad/amix/apad filters this module
# depends on) — no separate system ffmpeg install is needed on the host or
# in the Docker image.
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

AUDIO_SAMPLE_RATE, AUDIO_CHANNELS = AudioQuality.HIGH.value


class RecorderError(RuntimeError):
    pass


class AlreadyRecordingError(RecorderError):
    pass


class NoActiveCallError(RecorderError):
    pass


@dataclass
class _AudioTrack:
    path: Path
    handle: object


@dataclass
class _VideoTrack:
    path: Path
    handle: object
    width: int = 0
    height: int = 0
    frame_count: int = 0
    first_ts_ms: int = 0
    last_ts_ms: int = 0


@dataclass
class RecordingSession:
    recording_id: int
    chat_id: int
    kind: RecordingKind
    work_dir: Path
    started_by_id: int
    worker_id: int
    call_py: PyTgCalls
    client: Client
    started_monotonic: float = field(default_factory=time.monotonic)
    ssrc_to_user: dict[int, int] = field(default_factory=dict)
    audio_tracks: dict[int, _AudioTrack] = field(default_factory=dict)
    video_tracks: dict[int, _VideoTrack] = field(default_factory=dict)
    watchdog: asyncio.Task | None = None


_sessions: dict[int, RecordingSession] = {}


def is_recording(chat_id: int) -> bool:
    return chat_id in _sessions


def active_chat_ids() -> list[int]:
    return list(_sessions.keys())


# ---------------------------------------------------------------------------
# Participant bookkeeping (ssrc -> user_id), used to name/attribute tracks
# ---------------------------------------------------------------------------


async def _refresh_participants(chat_id: int) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    try:
        participants = await session.call_py.get_participants(chat_id)
    except Exception as exc:  # call may have just ended
        log.debug("get_participants(%s) failed: %s", chat_id, exc)
        return
    for p in participants:
        session.ssrc_to_user[p.source] = p.user_id


async def _on_participant_update(_client, update: UpdatedGroupCallParticipant) -> None:
    session = _sessions.get(update.chat_id)
    if session:
        session.ssrc_to_user[update.participant.source] = update.participant.user_id


# ---------------------------------------------------------------------------
# Frame callbacks — the actual capture
# ---------------------------------------------------------------------------


async def _on_audio_frame(_client, update: StreamFrames) -> None:
    session = _sessions.get(update.chat_id)
    if not session:
        return
    for frame in update.frames:
        track = session.audio_tracks.get(frame.ssrc)
        if track is None:
            path = session.work_dir / f"audio_{frame.ssrc}.raw"
            track = _AudioTrack(path=path, handle=open(path, "ab"))
            session.audio_tracks[frame.ssrc] = track
            if frame.ssrc not in session.ssrc_to_user:
                asyncio.create_task(_refresh_participants(update.chat_id))
        track.handle.write(frame.frame)


async def _on_video_frame(_client, update: StreamFrames) -> None:
    session = _sessions.get(update.chat_id)
    if not session or session.kind != RecordingKind.VIDEO:
        return
    for frame in update.frames:
        track = session.video_tracks.get(frame.ssrc)
        if track is None:
            path = session.work_dir / f"video_{frame.ssrc}.raw"
            track = _VideoTrack(
                path=path,
                handle=open(path, "ab"),
                width=frame.info.width,
                height=frame.info.height,
                first_ts_ms=frame.info.capture_time,
            )
            session.video_tracks[frame.ssrc] = track
            if frame.ssrc not in session.ssrc_to_user:
                asyncio.create_task(_refresh_participants(update.chat_id))
        track.handle.write(frame.frame)
        track.frame_count += 1
        track.last_ts_ms = frame.info.capture_time or track.last_ts_ms


def register_handlers(call_py_instance: PyTgCalls) -> None:
    """Binds the callbacks above to one worker's PyTgCalls instance. Called
    once per worker as it starts (userbot/pool.py) — with N workers there
    are N separate PyTgCalls instances, each needing its own registration,
    which is why these are plain functions instead of @call_py.on_update
    decorators on a single module-level instance."""
    call_py_instance.on_update(pfilters.call_participant())(_on_participant_update)
    call_py_instance.on_update(pfilters.stream_frame(Direction.INCOMING, Device.MICROPHONE))(_on_audio_frame)
    call_py_instance.on_update(pfilters.stream_frame(Direction.INCOMING, Device.CAMERA))(_on_video_frame)


# ---------------------------------------------------------------------------
# Lifecycle: start / stop
# ---------------------------------------------------------------------------


async def _announce(client: Client, chat_id: int, text: str) -> None:
    if not config.ANNOUNCE_RECORDING:
        return
    with contextlib.suppress(RPCError):
        await client.send_message(chat_id, text)


async def start_recording(
    chat_id: int,
    started_by_id: int,
    kind: RecordingKind,
    max_minutes: int,
    worker_id: int,
    call_py: PyTgCalls,
    client: Client,
    storage_days: int = config.FREE_STORAGE_DAYS,
) -> Recording:
    if chat_id in _sessions:
        raise AlreadyRecordingError("Recording is already running in this chat")

    rec = await crud.create_recording(chat_id, started_by_id, kind)
    work_dir = config.RECORDINGS_DIR / f"rec_{rec.id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    session = RecordingSession(
        recording_id=rec.id,
        chat_id=chat_id,
        kind=kind,
        work_dir=work_dir,
        started_by_id=started_by_id,
        worker_id=worker_id,
        call_py=call_py,
        client=client,
    )
    _sessions[chat_id] = session

    try:
        await call_py.record(
            chat_id,
            RecordStream(
                audio=True,
                audio_parameters=AudioQuality.HIGH,
                camera=(kind == RecordingKind.VIDEO),
            ),
        )
    except Exception as exc:
        _sessions.pop(chat_id, None)
        await crud.finish_recording(
            rec.id, file_path=None, duration_seconds=0, file_size_bytes=0,
            participants_count=0, status=RecordingStatus.FAILED,
        )
        raise NoActiveCallError(
            "Не удалось подключиться к звонку — убедитесь, что голосовой/видеочат сейчас активен"
        ) from exc

    await _refresh_participants(chat_id)

    kind_label = "аудио- и видеочат" if kind == RecordingKind.VIDEO else "голосовой чат"
    await _announce(
        client, chat_id,
        f"🔴 Запись начата\n"
        f"Бот записывает {kind_label} этой группы по команде участника. "
        f"Запись автоматически остановится через {max_minutes} мин.\n"
        f"Остановить: /stoprecord",
    )

    session.watchdog = asyncio.create_task(_watchdog(chat_id, max_minutes, storage_days))
    return rec


async def _watchdog(chat_id: int, max_minutes: int, storage_days: int) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.sleep(max_minutes * 60)
        if chat_id in _sessions:
            log.info("Recording in %s hit the %s-minute limit, auto-stopping", chat_id, max_minutes)
            await stop_recording(chat_id, storage_days=storage_days)


async def stop_recording(chat_id: int, storage_days: int) -> Recording:
    session = _sessions.pop(chat_id, None)
    if session is None:
        raise RecorderError("В этом чате сейчас ничего не записывается")

    if session.watchdog:
        session.watchdog.cancel()

    with contextlib.suppress(Exception):
        await session.call_py.leave_call(chat_id)

    for track in session.audio_tracks.values():
        track.handle.close()
    for track in session.video_tracks.values():
        track.handle.close()

    await crud.finish_recording(
        session.recording_id, file_path=None, duration_seconds=0, file_size_bytes=0,
        participants_count=0, status=RecordingStatus.PROCESSING,
    )
    await _announce(session.client, chat_id, "⏹ Запись остановлена, обрабатываю файл…")

    final_path, extra_paths, duration_s = await asyncio.to_thread(_mixdown, session)
    size_bytes = final_path.stat().st_size if final_path and final_path.exists() else 0
    participants = len(session.ssrc_to_user) or len(session.audio_tracks) or len(session.video_tracks)

    storage_backend = StorageBackend.LOCAL
    stored_path: str | None = None
    stored_extras: list[str] = []
    if final_path and final_path.exists():
        stored_path, storage_backend = await filestore.store_recording(
            final_path, f"recordings/rec_{session.recording_id}/{final_path.name}"
        )
        for extra in extra_paths:
            if extra.exists():
                extra_key, _backend = await filestore.store_recording(
                    extra, f"recordings/rec_{session.recording_id}/{extra.name}"
                )
                stored_extras.append(extra_key)

    rec = await crud.finish_recording(
        session.recording_id,
        file_path=stored_path,
        duration_seconds=duration_s,
        file_size_bytes=size_bytes,
        participants_count=participants,
        extra_files=stored_extras,
        status=RecordingStatus.DONE if stored_path else RecordingStatus.FAILED,
        storage_days=storage_days,
        storage_backend=storage_backend,
    )

    for track in list(session.audio_tracks.values()) + list(session.video_tracks.values()):
        with contextlib.suppress(OSError):
            track.path.unlink()

    return rec


# ---------------------------------------------------------------------------
# ffmpeg mixdown (runs in a worker thread — see stop_recording)
# ---------------------------------------------------------------------------


def _run_ffmpeg(args: list[str]) -> bool:
    import subprocess

    result = subprocess.run(
        [FFMPEG_EXE, "-y", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        log.warning("ffmpeg failed: %s", result.stderr.decode(errors="ignore")[-2000:])
    return result.returncode == 0


def _mixdown(session: RecordingSession) -> tuple[Path | None, list[Path], int]:
    work_dir = session.work_dir
    duration_s = int(time.monotonic() - session.started_monotonic)

    if session.kind == RecordingKind.VIDEO and session.video_tracks:
        return _mixdown_video(session, duration_s)
    return _mixdown_audio(session, duration_s)


def _encode_participant_audio_tracks(work_dir: Path, session: RecordingSession) -> list[tuple[int, Path]]:
    """Encodes every non-empty raw audio track to its own Opus file.
    Returns (ssrc, path) pairs so callers can still resolve usernames."""
    outputs: list[tuple[int, Path]] = []
    for ssrc, track in session.audio_tracks.items():
        if track.path.stat().st_size == 0:
            continue
        user_id = session.ssrc_to_user.get(ssrc, ssrc)
        out = work_dir / f"track_{user_id}.ogg"
        ok = _run_ffmpeg([
            "-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS),
            "-i", str(track.path),
            "-c:a", "libopus", "-b:a", "64k",
            str(out),
        ])
        if ok:
            outputs.append((ssrc, out))
    return outputs


def _mix_audio_only(work_dir: Path, per_track_outputs: list[Path], out_name: str = "audio_mix.ogg") -> Path | None:
    """Combines already-encoded per-participant Opus tracks into one mixed
    track. Used both as the final output for audio-only recordings and as
    the audio bed under the video grid — everyone's voice should be audible
    in the video export too, not just whoever had their camera on."""
    if not per_track_outputs:
        return None
    final = work_dir / out_name
    if len(per_track_outputs) == 1:
        import shutil
        shutil.copyfile(per_track_outputs[0], final)
        return final

    inputs: list[str] = []
    for p in per_track_outputs:
        inputs += ["-i", str(p)]
    filter_complex = f"amix=inputs={len(per_track_outputs)}:duration=longest:normalize=0"
    if _run_ffmpeg([*inputs, "-filter_complex", filter_complex, "-c:a", "libopus", "-b:a", "96k", str(final)]):
        return final
    return per_track_outputs[0]  # fall back to the loudest single track rather than nothing


def _mixdown_audio(session: RecordingSession, duration_s: int) -> tuple[Path | None, list[Path], int]:
    work_dir = session.work_dir
    per_track = _encode_participant_audio_tracks(work_dir, session)
    per_track_outputs = [p for _ssrc, p in per_track]

    if not per_track_outputs:
        return None, [], duration_s

    if len(per_track_outputs) == 1:
        final = work_dir / "recording.ogg"
        per_track_outputs[0].replace(final)
        return final, [], duration_s

    final = _mix_audio_only(work_dir, per_track_outputs, out_name="recording.ogg")

    extra_zip = work_dir / "tracks_multitrack.zip"
    with zipfile.ZipFile(extra_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in per_track_outputs:
            zf.write(p, arcname=p.name)

    return final, [extra_zip], duration_s


GRID_CELL_W = 480
GRID_CELL_H = 360
GRID_FPS = 24


def _encode_participant_video(work_dir: Path, session: RecordingSession, ssrc: int, vtrack: _VideoTrack) -> tuple[Path, float] | None:
    """Encodes one participant's raw camera stream (+ their own mic, if any)
    to an individual mp4. Returns (path, duration_seconds) or None if this
    track never actually received usable frames."""
    if vtrack.frame_count == 0 or not vtrack.width or not vtrack.height:
        return None
    user_id = session.ssrc_to_user.get(ssrc, ssrc)
    span_ms = max(vtrack.last_ts_ms - vtrack.first_ts_ms, 1)
    fps = max(1.0, min(30.0, vtrack.frame_count / (span_ms / 1000)))
    track_duration_s = span_ms / 1000

    atrack = session.audio_tracks.get(ssrc)
    out = work_dir / f"video_{user_id}.mp4"
    args = [
        "-f", "rawvideo", "-pixel_format", "yuv420p",
        "-video_size", f"{vtrack.width}x{vtrack.height}", "-framerate", f"{fps:.2f}",
        "-i", str(vtrack.path),
    ]
    if atrack and atrack.path.stat().st_size > 0:
        args += ["-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-i", str(atrack.path)]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(out)]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-an", str(out)]

    return (out, track_duration_s) if _run_ffmpeg(args) else None


def _grid_layout(n: int) -> tuple[int, int]:
    """(cols, rows) for a roughly-square gallery grid — same approach video
    conferencing gallery views use (1x2 for two people, 2x2 for three or
    four, 3x2 for five or six, ...)."""
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return cols, rows


def _build_video_grid(work_dir: Path, clips: list[Path], durations: list[float], mixed_audio: Path | None) -> Path | None:
    """Composites 2+ already-encoded participant clips into one gallery-style
    grid (à la Zoom/Meet), soundtracked by everyone's combined voice. A clip
    that ends early is frozen on its last frame (`tpad`) rather than cutting
    the whole grid short the moment the first person leaves."""
    n = len(clips)
    if n == 0:
        return None
    cols, rows = _grid_layout(n)
    max_duration = max(durations) if durations else 0.0

    filter_parts = []
    tile_labels = []
    for i, (clip, dur) in enumerate(zip(clips, durations)):
        pad_needed = max(0.0, max_duration - dur)
        chain = (
            f"[{i}:v]scale={GRID_CELL_W}:{GRID_CELL_H}:force_original_aspect_ratio=decrease,"
            f"pad={GRID_CELL_W}:{GRID_CELL_H}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
        if pad_needed > 0.05:
            chain += f",tpad=stop_mode=clone:stop_duration={pad_needed:.2f}"
        chain += f",fps={GRID_FPS},setpts=PTS-STARTPTS[v{i}]"
        filter_parts.append(chain)
        tile_labels.append(f"[v{i}]")

    layout = "|".join(f"{(i % cols) * GRID_CELL_W}_{(i // cols) * GRID_CELL_H}" for i in range(n))
    filter_parts.append(f"{''.join(tile_labels)}xstack=inputs={n}:layout={layout}:fill=black[grid]")

    inputs: list[str] = []
    for clip in clips:
        inputs += ["-i", str(clip)]
    has_audio = bool(mixed_audio and mixed_audio.exists() and mixed_audio.stat().st_size > 0)
    if has_audio:
        inputs += ["-i", str(mixed_audio)]
        # Pad with silence rather than using -shortest: the video side is
        # already extended to max_duration via tpad above, so truncating to
        # whichever mixed-audio track happens to be shorter would silently
        # crop off real video content instead.
        filter_parts.append(f"[{n}:a]apad[aout]")

    filter_complex = ";".join(filter_parts)

    out = work_dir / "grid.mp4"
    args = [*inputs, "-filter_complex", filter_complex, "-map", "[grid]"]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-t", f"{max(max_duration, 0.1):.2f}", str(out)]

    return out if _run_ffmpeg(args) else None


def _mixdown_video(session: RecordingSession, duration_s: int) -> tuple[Path | None, list[Path], int]:
    work_dir = session.work_dir
    clips: list[Path] = []
    durations: list[float] = []

    for ssrc, vtrack in session.video_tracks.items():
        encoded = _encode_participant_video(work_dir, session, ssrc, vtrack)
        if encoded:
            path, dur = encoded
            clips.append(path)
            durations.append(dur)

    if not clips:
        # camera never actually started (common if nobody turned video on) — fall back to audio-only
        return _mixdown_audio(session, duration_s)

    # Everyone's voice belongs on the video export, including anyone who
    # spoke but never turned their camera on.
    audio_tracks = _encode_participant_audio_tracks(work_dir, session)
    mixed_audio = _mix_audio_only(work_dir, [p for _ssrc, p in audio_tracks], out_name="grid_audio.ogg")

    extra: list[Path] = []
    if len(clips) > 1:
        extra_zip = work_dir / "camera_tracks.zip"
        with zipfile.ZipFile(extra_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in clips:
                zf.write(p, arcname=p.name)
        extra.append(extra_zip)

    if len(clips) == 1:
        final = clips[0]
        if mixed_audio:
            muxed = work_dir / "final.mp4"
            video_duration = durations[0] if durations else 0.0
            ok = _run_ffmpeg([
                "-i", str(clips[0]), "-i", str(mixed_audio),
                "-filter_complex", "[1:a]apad[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-t", f"{max(video_duration, 0.1):.2f}", str(muxed),
            ])
            if ok:
                final = muxed
        return final, extra, duration_s

    grid = _build_video_grid(work_dir, clips, durations, mixed_audio)
    if grid is None:
        # Grid compositing failed for some reason (e.g. an unsupported
        # resolution) — the individual camera tracks are already zipped in
        # `extra`, so still deliver the first participant's clip rather than
        # nothing.
        log.warning("Grid compositing failed for recording in work_dir=%s, falling back to a single clip", work_dir)
        return clips[0], extra, duration_s

    return grid, extra, duration_s
