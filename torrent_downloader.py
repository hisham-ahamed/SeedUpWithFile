"""
SeedUp - Smart Torrent Management Tool
Torrent downloader module using libtorrent with resume capability.

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

import libtorrent as lt
import time
import os
import sys
from config import TORRENT_SESSION_FILE, TORRENT_DOWNLOAD_PATH, get_logger

logger = get_logger(__name__)

# Check if running in Google Colab
try:
    from google.colab import files
    IN_COLAB = True
except ImportError:
    IN_COLAB = False


def save_session(session, session_file=TORRENT_SESSION_FILE):
    """Save session state to resume later (correctly saves binary data)."""
    try:
        with open(session_file, "wb") as f:
            session_state = session.save_state()
            f.write(lt.bencode(session_state))
        logger.debug(f"Session saved to {session_file}")
    except Exception as e:
        logger.error(f"Failed to save session: {e}")


def load_session(session_file=TORRENT_SESSION_FILE):
    """Load session state if it exists, otherwise return a new session."""
    if os.path.exists(session_file):
        try:
            with open(session_file, "rb") as f:
                session_data = f.read()
                if not session_data:
                    raise ValueError("Session file is empty.")

                session_state = lt.bdecode(session_data)
                ses = lt.session()
                ses.load_state(session_state)
                logger.info(f"Session loaded from {session_file}")
                return ses
        except (RuntimeError, ValueError) as e:
            logger.warning(f"Failed to load session ({e}). Starting fresh.")
            os.remove(session_file)

    return lt.session()


def _add_torrent(session, source, download_path):
    """Add a magnet link or .torrent file to a libtorrent session."""
    params = lt.add_torrent_params()
    params.save_path = download_path
    params.storage_mode = lt.storage_mode_t.storage_mode_sparse

    if source.startswith("magnet:"):
        params.url = source
        logger.info(f"Adding magnet link: {source[:60]}...")
    elif source.endswith(".torrent"):
        if not os.path.exists(source):
            raise FileNotFoundError(f"Torrent file not found: {source}")

        with open(source, "rb") as f:
            torrent_data = lt.bdecode(f.read())
            params.ti = lt.torrent_info(torrent_data)
        logger.info(f"Adding torrent file: {source}")
    else:
        raise ValueError("Invalid source. Provide a .torrent file or magnet link.")

    return session.add_torrent(params)


def _wait_for_metadata(handle, timeout=None):
    """Wait until torrent metadata is available."""
    logger.info("Waiting for metadata...")
    started = time.time()

    while not handle.status().has_metadata:
        if timeout is not None and time.time() - started > timeout:
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


def _format_bytes(size):
    """Return a readable byte-size string."""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024


def get_torrent_files(source, metadata_timeout=180):
    """
    Fetch torrent metadata and return its files without downloading the payload.

    File numbers returned to the user are 1-based.
    """
    os.makedirs(TORRENT_DOWNLOAD_PATH, exist_ok=True)

    ses = lt.session()
    ses.apply_settings({'listen_interfaces': '0.0.0.0:6881'})

    handle = None
    try:
        handle = _add_torrent(ses, source, TORRENT_DOWNLOAD_PATH)
        _wait_for_metadata(handle, timeout=metadata_timeout)
        handle.pause()

        info = _get_torrent_info(handle)
        storage = info.files()

        torrent_files = []
        for index in range(storage.num_files()):
            torrent_files.append({
                'number': index + 1,
                'index': index,
                'path': storage.file_path(index),
                'size': storage.file_size(index),
            })
        return torrent_files
    finally:
        if handle is not None:
            try:
                ses.remove_torrent(handle)
            except Exception:
                pass


def print_torrent_files(source, metadata_timeout=180):
    """Print a numbered file list and return it."""
    torrent_files = get_torrent_files(source, metadata_timeout=metadata_timeout)

    print("\n" + "=" * 90)
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
    print("Leave the selection empty to download every file.\n")
    return torrent_files


def parse_file_selection(selection, total_files):
    """
    Convert a 1-based selection such as '1,3,7-10' into zero-based indexes.

    Blank, None, '*' and 'all' mean all files and return None.
    """
    if selection is None:
        return None

    selection = str(selection).strip()
    if not selection or selection.lower() in {'all', '*'}:
        return None

    selected = set()
    for token in selection.split(','):
        token = token.strip()
        if not token:
            continue

        try:
            if '-' in token:
                start_text, end_text = token.split('-', 1)
                start = int(start_text.strip())
                end = int(end_text.strip())
                if start > end:
                    start, end = end, start
                selected.update(range(start, end + 1))
            else:
                selected.add(int(token))
        except ValueError as exc:
            raise ValueError(
                f"Invalid file selection '{token}'. Use values like 1,3,7-10."
            ) from exc

    if not selected:
        raise ValueError("No valid file numbers were selected.")

    invalid = sorted(number for number in selected if number < 1 or number > total_files)
    if invalid:
        raise ValueError(
            f"File number(s) out of range: {invalid}. Valid range is 1-{total_files}."
        )

    return sorted(number - 1 for number in selected)


def _apply_file_selection(handle, selection):
    """Set unselected files to priority 0 and selected files to normal priority."""
    info = _get_torrent_info(handle)
    storage = info.files()
    total_files = storage.num_files()
    selected_indexes = parse_file_selection(selection, total_files)

    if selected_indexes is None:
        return None, [
            os.path.normpath(storage.file_path(i)) for i in range(total_files)
        ]

    handle.pause()
    priorities = [0] * total_files
    for index in selected_indexes:
        priorities[index] = 4
    handle.prioritize_files(priorities)
    handle.resume()

    selected_paths = [
        os.path.normpath(storage.file_path(i)) for i in selected_indexes
    ]
    selected_size = sum(storage.file_size(i) for i in selected_indexes)

    logger.info(
        f"Selected {len(selected_indexes)} of {total_files} files "
        f"({_format_bytes(selected_size)})"
    )
    for index in selected_indexes:
        logger.info(f"  [{index + 1}] {storage.file_path(index)}")

    return set(selected_indexes), selected_paths


def _remove_unselected_files(download_path, handle, selected_indexes):
    """Delete zero-priority files/fragments before Google Drive upload."""
    if selected_indexes is None:
        return

    info = _get_torrent_info(handle)
    storage = info.files()
    torrent_root = os.path.abspath(
        os.path.join(download_path, handle.status().name)
    )

    for index in range(storage.num_files()):
        if index in selected_indexes:
            continue

        candidate = os.path.abspath(
            os.path.join(download_path, storage.file_path(index))
        )

        # Safety: never delete outside the configured download directory.
        if os.path.commonpath([os.path.abspath(download_path), candidate]) != os.path.abspath(download_path):
            logger.warning(f"Skipped unsafe cleanup path: {candidate}")
            continue

        if os.path.isfile(candidate):
            try:
                os.remove(candidate)
            except OSError as exc:
                logger.warning(f"Could not remove unselected file {candidate}: {exc}")

    # Remove empty directories below the torrent root, but keep the root itself.
    if os.path.isdir(torrent_root):
        for root, dirs, files_in_dir in os.walk(torrent_root, topdown=False):
            if os.path.abspath(root) == torrent_root:
                continue
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass


def download_torrent(source, download_path=TORRENT_DOWNLOAD_PATH,
                     session_file=TORRENT_SESSION_FILE, auto_resume=True,
                     file_selection=None):
    """
    Download a torrent file using libtorrent, with support for selective files.

    :param source: .torrent file path or magnet link.
    :param download_path: Directory to save the downloaded content.
    :param session_file: File to save/load session state.
    :param auto_resume: Automatically load previous session if available.
    :param file_selection: 1-based file numbers/ranges, e.g. '1,3,7-10'.
                           Blank/None downloads all files.
    :return: Path to downloaded content or None on failure.
    """
    if not os.path.exists(download_path):
        os.makedirs(download_path)
        logger.info(f"Created download directory: {download_path}")

    is_resuming = auto_resume and os.path.exists(session_file)
    ses = load_session(session_file) if auto_resume else lt.session()
    ses.apply_settings({'listen_interfaces': '0.0.0.0:6881'})

    try:
        handle = _add_torrent(ses, source, download_path)
        logger.info(f"Downloading to: {download_path}")
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        return None
    except Exception as e:
        logger.error(f"Failed to add torrent: {e}")
        return None

    try:
        _wait_for_metadata(handle)
        torrent_name = handle.status().name
        logger.info(f"Downloading: {torrent_name}")

        selected_indexes, _ = _apply_file_selection(handle, file_selection)

        while not handle.status().is_seeding:
            s = handle.status()
            progress = s.progress * 100

            eta_str = "N/A"
            if s.download_rate > 0:
                total_size = s.total_wanted
                downloaded = s.total_wanted_done
                remaining = max(0, total_size - downloaded)
                eta_seconds = remaining / s.download_rate

                if eta_seconds < 60:
                    eta_str = f"{int(eta_seconds)}s"
                elif eta_seconds < 3600:
                    eta_str = f"{int(eta_seconds / 60)}m {int(eta_seconds % 60)}s"
                else:
                    hours = int(eta_seconds / 3600)
                    minutes = int((eta_seconds % 3600) / 60)
                    eta_str = f"{hours}h {minutes}m"

            if s.download_rate > 1024 * 1024:
                speed_str = f"{s.download_rate / (1024 * 1024):.2f} MB/s"
            else:
                speed_str = f"{s.download_rate / 1024:.2f} KB/s"

            bar_length = 30
            filled_length = int(bar_length * progress / 100)
            bar = '█' * filled_length + '░' * (bar_length - filled_length)

            if is_resuming and progress < 95:
                label = "Resuming Download"
            elif s.download_rate == 0 and s.num_peers == 0:
                label = "Connecting to Peers"
            else:
                label = "Download Progress"
                is_resuming = False

            peer_count = max(0, s.num_peers - s.num_seeds)
            stats_str = (
                f"Seeds: {s.num_seeds} | Peers: {peer_count} | "
                f"Speed: {speed_str} | ETA: {eta_str}"
            )
            progress_line = (
                f"{label}: {bar} {progress:.1f}/100%    | {stats_str}"
            )
            print(f"\r{progress_line}", end="", flush=True)

            if int(time.time()) % 10 == 0:
                save_session(ses, session_file)

            time.sleep(1)

    except KeyboardInterrupt:
        print()
        logger.warning("Download paused by user. Session saved for resume.")
        save_session(ses, session_file)
        return None
    except Exception as e:
        print()
        logger.error(f"Download failed: {e}")
        save_session(ses, session_file)
        return None

    print()
    logger.info("Download complete!")

    _remove_unselected_files(download_path, handle, selected_indexes)

    if os.path.exists(session_file):
        try:
            os.remove(session_file)
            logger.debug("Session file removed after successful download")
        except Exception as e:
            logger.warning(f"Could not remove session file: {e}")

    return os.path.join(download_path, torrent_name)


def get_download_status(session_file=TORRENT_SESSION_FILE):
    """Return True if a resumable session file exists."""
    return os.path.exists(session_file)


def clear_session(session_file=TORRENT_SESSION_FILE):
    """Clear the session file to start fresh."""
    if os.path.exists(session_file):
        try:
            os.remove(session_file)
            logger.info("Session file cleared")
            return True
        except Exception as e:
            logger.error(f"Failed to clear session: {e}")
            return False
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python torrent_downloader.py <torrent_file/magnet_link>")
        sys.exit(1)

    source = sys.argv[1]
    result = download_torrent(source)

    if result:
        print(f"\nDownloaded to: {result}")
        sys.exit(0)
    sys.exit(1)
