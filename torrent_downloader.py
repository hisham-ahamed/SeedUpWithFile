"""
SeedUp - Smart Torrent Management Tool
Torrent downloader module using libtorrent with resume and selective-file support.

Copyright 2025 Ishara Deshapriya

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import sys
import time
from typing import List, Optional, Set, Tuple

import libtorrent as lt

from config import TORRENT_DOWNLOAD_PATH, TORRENT_SESSION_FILE, get_logger


logger = get_logger(__name__)


def save_session(session, session_file: str = TORRENT_SESSION_FILE) -> None:
    """Save libtorrent session state to disk."""
    try:
        session_state = session.save_state()
        encoded_state = lt.bencode(session_state)

        with open(session_file, "wb") as file_obj:
            file_obj.write(encoded_state)

        logger.debug("Session saved to %s", session_file)

    except Exception as exc:
        logger.error("Failed to save session: %s", exc)


def load_session(session_file: str = TORRENT_SESSION_FILE):
    """Load saved libtorrent session state or return a fresh session."""
    if os.path.exists(session_file):
        try:
            with open(session_file, "rb") as file_obj:
                session_data = file_obj.read()

            if not session_data:
                raise ValueError("Session file is empty.")

            decoded_state = lt.bdecode(session_data)

            session = lt.session()
            session.load_state(decoded_state)

            logger.info("Session loaded from %s", session_file)
            return session

        except Exception as exc:
            logger.warning(
                "Could not load session (%s). Starting a fresh session.",
                exc,
            )

            try:
                os.remove(session_file)
            except OSError:
                pass

    return lt.session()


def _configure_session(session) -> None:
    """Apply common libtorrent session settings."""
    session.apply_settings({
        "listen_interfaces": "0.0.0.0:6881",
    })


def _add_torrent(session, source: str, download_path: str):
    """Add a magnet link or local .torrent file to the session."""
    source = str(source).strip()

    params = lt.add_torrent_params()
    params.save_path = os.path.abspath(download_path)
    params.storage_mode = lt.storage_mode_t.storage_mode_sparse

    if source.startswith("magnet:"):
        params.url = source
        logger.info("Adding magnet link: %s...", source[:60])

    elif source.lower().endswith(".torrent"):
        if not os.path.isfile(source):
            raise FileNotFoundError(f"Torrent file not found: {source}")

        with open(source, "rb") as file_obj:
            torrent_data = lt.bdecode(file_obj.read())

        params.ti = lt.torrent_info(torrent_data)
        logger.info("Adding torrent file: %s", source)

    else:
        raise ValueError(
            "Invalid torrent source. Provide a magnet link or a .torrent file."
        )

    return session.add_torrent(params)


def _wait_for_metadata(handle, timeout: Optional[int] = None) -> None:
    """Wait until torrent metadata becomes available."""
    logger.info("Waiting for metadata...")
    started_at = time.monotonic()

    while not handle.status().has_metadata:
        if (
            timeout is not None
            and time.monotonic() - started_at >= timeout
        ):
            raise TimeoutError(
                f"Torrent metadata was not received within {timeout} seconds."
            )

        time.sleep(1)


def _get_torrent_info(handle):
    """Return torrent_info across libtorrent Python binding versions."""
    try:
        return handle.torrent_file()
    except AttributeError:
        return handle.get_torrent_info()


def _format_bytes(size: int) -> str:
    """Convert a byte value to a readable string."""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(0, int(size)))

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} TB"


def get_torrent_files(
    source: str,
    metadata_timeout: int = 180,
) -> List[dict]:
    """
    Fetch torrent metadata and return all files without downloading the payload.

    File numbers returned to the user are 1-based.
    """
    os.makedirs(TORRENT_DOWNLOAD_PATH, exist_ok=True)

    session = lt.session()
    _configure_session(session)

    handle = None

    try:
        handle = _add_torrent(
            session,
            source,
            TORRENT_DOWNLOAD_PATH,
        )

        _wait_for_metadata(
            handle,
            timeout=metadata_timeout,
        )

        handle.pause()

        torrent_info = _get_torrent_info(handle)
        storage = torrent_info.files()

        torrent_files = []

        for index in range(storage.num_files()):
            torrent_files.append({
                "number": index + 1,
                "index": index,
                "path": storage.file_path(index),
                "size": storage.file_size(index),
            })

        return torrent_files

    finally:
        if handle is not None:
            try:
                session.remove_torrent(handle)
            except Exception:
                pass


def print_torrent_files(
    source: str,
    metadata_timeout: int = 180,
) -> List[dict]:
    """Print a numbered list of files inside the torrent."""
    torrent_files = get_torrent_files(
        source,
        metadata_timeout=metadata_timeout,
    )

    print()
    print("=" * 90)
    print("FILES IN TORRENT")
    print("=" * 90)

    for item in torrent_files:
        print(
            f"[{item['number']:>4}] "
            f"{_format_bytes(item['size']):>12}  "
            f"{item['path']}"
        )

    print("=" * 90)
    print("Select files using numbers such as: 1,3,7-10")
    print("Leave the selection empty to download every file.")
    print()

    return torrent_files


def parse_file_selection(
    selection: Optional[str],
    total_files: int,
) -> Optional[List[int]]:
    """
    Convert a 1-based selection such as 1,3,7-10 into zero-based indexes.

    Blank, None, "all", or "*" means download all files.
    """
    if selection is None:
        return None

    selection = str(selection).strip()

    if not selection or selection.lower() in {"all", "*"}:
        return None

    selected_numbers: Set[int] = set()

    for token in selection.split(","):
        token = token.strip()

        if not token:
            continue

        try:
            if "-" in token:
                start_text, end_text = token.split("-", 1)

                start = int(start_text.strip())
                end = int(end_text.strip())

                if start > end:
                    start, end = end, start

                selected_numbers.update(range(start, end + 1))

            else:
                selected_numbers.add(int(token))

        except ValueError as exc:
            raise ValueError(
                f"Invalid file selection '{token}'. "
                "Use a format such as 1,3,7-10."
            ) from exc

    if not selected_numbers:
        raise ValueError("No valid file numbers were selected.")

    invalid_numbers = sorted(
        number
        for number in selected_numbers
        if number < 1 or number > total_files
    )

    if invalid_numbers:
        raise ValueError(
            f"File number(s) out of range: {invalid_numbers}. "
            f"Valid range is 1-{total_files}."
        )

    return sorted(number - 1 for number in selected_numbers)


def _apply_file_selection(
    handle,
    selection: Optional[str],
) -> Tuple[Optional[Set[int]], int]:
    """
    Set selected files to normal priority and unselected files to priority zero.

    Returns:
        A set of selected zero-based indexes, or None when all files are wanted.
        The total selected/wanted size in bytes.
    """
    torrent_info = _get_torrent_info(handle)
    storage = torrent_info.files()
    total_files = storage.num_files()

    selected_indexes = parse_file_selection(
        selection,
        total_files,
    )

    if selected_indexes is None:
        wanted_size = sum(
            storage.file_size(index)
            for index in range(total_files)
        )

        logger.info(
            "No file filter supplied. Downloading all %s files (%s).",
            total_files,
            _format_bytes(wanted_size),
        )

        return None, wanted_size

    handle.pause()

    priorities = [0] * total_files

    for index in selected_indexes:
        priorities[index] = 4

    handle.prioritize_files(priorities)
    handle.resume()

    wanted_size = sum(
        storage.file_size(index)
        for index in selected_indexes
    )

    logger.info(
        "Selected %s of %s files (%s).",
        len(selected_indexes),
        total_files,
        _format_bytes(wanted_size),
    )

    for index in selected_indexes:
        logger.info(
            "  [%s] %s",
            index + 1,
            storage.file_path(index),
        )

    return set(selected_indexes), wanted_size


def _remove_unselected_files(
    download_path: str,
    handle,
    selected_indexes: Optional[Set[int]],
) -> None:
    """
    Remove unselected file fragments before upload.

    Some torrent pieces may overlap file boundaries, so small fragments of
    unselected files can still be created by libtorrent.
    """
    if selected_indexes is None:
        return

    download_root = os.path.abspath(download_path)

    torrent_info = _get_torrent_info(handle)
    storage = torrent_info.files()

    torrent_root = os.path.abspath(
        os.path.join(
            download_root,
            handle.status().name,
        )
    )

    for index in range(storage.num_files()):
        if index in selected_indexes:
            continue

        candidate = os.path.abspath(
            os.path.join(
                download_root,
                storage.file_path(index),
            )
        )

        try:
            is_safe_path = (
                os.path.commonpath([
                    download_root,
                    candidate,
                ])
                == download_root
            )
        except ValueError:
            is_safe_path = False

        if not is_safe_path:
            logger.warning(
                "Skipped unsafe cleanup path: %s",
                candidate,
            )
            continue

        if os.path.isfile(candidate):
            try:
                os.remove(candidate)
                logger.debug(
                    "Removed unselected file fragment: %s",
                    candidate,
                )
            except OSError as exc:
                logger.warning(
                    "Could not remove unselected file %s: %s",
                    candidate,
                    exc,
                )

    if os.path.isdir(torrent_root):
        for root, _directories, _files in os.walk(
            torrent_root,
            topdown=False,
        ):
            if os.path.abspath(root) == torrent_root:
                continue

            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass


def _download_is_complete(
    status,
    expected_wanted_size: int,
) -> bool:
    """
    Return True when all selected/wanted torrent data has finished.

    A selective torrent normally enters the "finished" state rather than
    "seeding", so checking only status.is_seeding can wait forever.
    """
    if bool(getattr(status, "is_seeding", False)):
        return True

    total_wanted = int(
        getattr(status, "total_wanted", 0) or 0
    )

    total_wanted_done = int(
        getattr(status, "total_wanted_done", 0) or 0
    )

    if total_wanted > 0 and total_wanted_done >= total_wanted:
        return True

    if (
        total_wanted == 0
        and expected_wanted_size == 0
    ):
        return True

    return False


def _make_progress_line(
    status,
    is_resuming: bool,
) -> str:
    """Build a Colab-friendly progress line based on wanted bytes."""
    total_wanted = int(
        getattr(status, "total_wanted", 0) or 0
    )

    total_wanted_done = int(
        getattr(status, "total_wanted_done", 0) or 0
    )

    download_rate = int(
        getattr(status, "download_rate", 0) or 0
    )

    if total_wanted > 0:
        progress = min(
            100.0,
            total_wanted_done * 100.0 / total_wanted,
        )
    else:
        progress = 0.0

    remaining = max(
        0,
        total_wanted - total_wanted_done,
    )

    eta_text = "N/A"

    if download_rate > 0:
        eta_seconds = remaining / download_rate

        if eta_seconds < 60:
            eta_text = f"{int(eta_seconds)}s"

        elif eta_seconds < 3600:
            eta_text = (
                f"{int(eta_seconds / 60)}m "
                f"{int(eta_seconds % 60)}s"
            )

        else:
            hours = int(eta_seconds / 3600)
            minutes = int(
                (eta_seconds % 3600) / 60
            )

            eta_text = f"{hours}h {minutes}m"

    if download_rate >= 1024 * 1024:
        speed_text = (
            f"{download_rate / (1024 * 1024):.2f} MB/s"
        )
    else:
        speed_text = (
            f"{download_rate / 1024:.2f} KB/s"
        )

    bar_length = 30

    filled_length = min(
        bar_length,
        int(bar_length * progress / 100.0),
    )

    progress_bar = (
        "█" * filled_length
        + "░" * (bar_length - filled_length)
    )

    number_of_seeds = max(
        0,
        int(getattr(status, "num_seeds", 0) or 0),
    )

    number_of_connections = max(
        0,
        int(getattr(status, "num_peers", 0) or 0),
    )

    number_of_peers = max(
        0,
        number_of_connections - number_of_seeds,
    )

    if is_resuming and progress < 99.9:
        label = "Resuming Download"

    elif download_rate == 0 and number_of_connections == 0:
        label = "Connecting to Peers"

    else:
        label = "Download Progress"

    return (
        f"{label}: "
        f"{progress_bar} "
        f"{progress:.1f}% | "
        f"{_format_bytes(total_wanted_done)} / "
        f"{_format_bytes(total_wanted)} | "
        f"Seeds: {number_of_seeds} | "
        f"Peers: {number_of_peers} | "
        f"Speed: {speed_text} | "
        f"ETA: {eta_text}"
    )


def download_torrent(
    source: str,
    download_path: str = TORRENT_DOWNLOAD_PATH,
    session_file: str = TORRENT_SESSION_FILE,
    auto_resume: bool = True,
    file_selection: Optional[str] = None,
):
    """
    Download a complete torrent or selected files from a torrent.

    Examples for file_selection:
        "1"
        "1,3,5"
        "1-7"
        "1,3,7-10"

    Blank or None downloads all files.
    """
    os.makedirs(download_path, exist_ok=True)

    is_resuming = (
        auto_resume
        and os.path.exists(session_file)
    )

    session = (
        load_session(session_file)
        if auto_resume
        else lt.session()
    )

    _configure_session(session)

    try:
        handle = _add_torrent(
            session,
            source,
            download_path,
        )

        logger.info(
            "Downloading to: %s",
            os.path.abspath(download_path),
        )

    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return None

    except Exception as exc:
        logger.error(
            "Failed to add torrent: %s",
            exc,
        )
        return None

    try:
        _wait_for_metadata(handle)

        torrent_name = handle.status().name
        logger.info("Downloading: %s", torrent_name)

        selected_indexes, expected_wanted_size = (
            _apply_file_selection(
                handle,
                file_selection,
            )
        )

        last_progress_output = 0.0
        last_session_save = 0.0

        while True:
            status = handle.status()

            if _download_is_complete(
                status,
                expected_wanted_size,
            ):
                break

            current_time = time.monotonic()

            if current_time - last_progress_output >= 5:
                print(
                    _make_progress_line(
                        status,
                        is_resuming,
                    ),
                    flush=True,
                )

                last_progress_output = current_time

                if int(
                    getattr(
                        status,
                        "download_rate",
                        0,
                    )
                    or 0
                ) > 0:
                    is_resuming = False

            if current_time - last_session_save >= 10:
                save_session(
                    session,
                    session_file,
                )

                last_session_save = current_time

            time.sleep(1)

    except KeyboardInterrupt:
        print()

        logger.warning(
            "Download paused by user. "
            "Session state was saved."
        )

        save_session(
            session,
            session_file,
        )

        return None

    except Exception as exc:
        print()

        logger.error(
            "Download failed: %s",
            exc,
        )

        save_session(
            session,
            session_file,
        )

        return None

    final_status = handle.status()

    print(
        _make_progress_line(
            final_status,
            False,
        ),
        flush=True,
    )

    logger.info("Selected download complete!")

    try:
        handle.pause()

        if hasattr(handle, "flush_cache"):
            handle.flush_cache()

        time.sleep(2)

    except Exception:
        pass

    _remove_unselected_files(
        download_path,
        handle,
        selected_indexes,
    )

    if os.path.exists(session_file):
        try:
            os.remove(session_file)
            logger.debug(
                "Session file removed after successful download."
            )
        except OSError as exc:
            logger.warning(
                "Could not remove session file: %s",
                exc,
            )

    return os.path.join(
        download_path,
        torrent_name,
    )


def get_download_status(
    session_file: str = TORRENT_SESSION_FILE,
) -> bool:
    """Return True when a resumable session file exists."""
    return os.path.exists(session_file)


def clear_session(
    session_file: str = TORRENT_SESSION_FILE,
) -> bool:
    """Delete the saved session file."""
    if not os.path.exists(session_file):
        return True

    try:
        os.remove(session_file)
        logger.info("Session file cleared.")
        return True

    except OSError as exc:
        logger.error(
            "Failed to clear session: %s",
            exc,
        )
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python torrent_downloader.py "
            "<torrent_file_or_magnet_link>"
        )
        sys.exit(1)

    result_path = download_torrent(
        sys.argv[1]
    )

    if result_path:
        print(f"\nDownloaded to: {result_path}")
        sys.exit(0)

    sys.exit(1)
