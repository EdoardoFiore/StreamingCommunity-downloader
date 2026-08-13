import functools
import logging
import os
import shutil

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def get_ffmpeg_exe() -> str:
    """Resolve the ffmpeg executable to invoke.

    Order: FFMPEG_PATH env override -> system PATH (apt/brew install, what the
    Docker image relies on) -> imageio-ffmpeg's bundled static binary. The last
    step exists because Windows has no equivalent of "apt install ffmpeg": users
    routinely have no ffmpeg on PATH at all, which surfaces deep inside a Join
    step as an opaque WinError 2 instead of a clear "install ffmpeg" message.
    """
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path:
        if shutil.which(env_path) or os.path.isfile(env_path):
            return env_path
        logger.warning("FFMPEG_PATH=%s is not a usable executable, ignoring", env_path)

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info("No system ffmpeg on PATH, using bundled binary: %s", bundled)
        return bundled
    except Exception as e:
        raise RuntimeError(
            "ffmpeg executable not found. Install ffmpeg and add it to PATH, "
            "or set the FFMPEG_PATH environment variable to its full path."
        ) from e


@functools.lru_cache(maxsize=1)
def get_ffprobe_exe() -> str | None:
    """Resolve ffprobe, or None when there is none to resolve.

    Unlike ffmpeg this is optional, and returns None rather than raising:
    imageio-ffmpeg bundles ffmpeg alone, so the Windows fallback that saves the
    download has no ffprobe to offer. Debian's ffmpeg package ships both, which
    covers the Docker image.

    FFPROBE_PATH overrides, then a sibling of the resolved ffmpeg — a manual
    install usually puts the pair in one directory, and finding ffmpeg there
    without looking would miss it — then PATH.
    """
    env_path = os.environ.get("FFPROBE_PATH")
    if env_path and (shutil.which(env_path) or os.path.isfile(env_path)):
        return env_path

    try:
        sibling = os.path.join(os.path.dirname(get_ffmpeg_exe()), "ffprobe")
    except RuntimeError:
        sibling = ""
    for candidate in (sibling, sibling + ".exe"):
        if candidate and os.path.isfile(candidate):
            return candidate

    return shutil.which("ffprobe")
