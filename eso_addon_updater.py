import argparse
import inspect
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from loguru import logger

PLUGINS_FILE = "plugins.json"


def get_dir_list(path: Path) -> dict[str, str]:
    dir_list = {}
    with os.scandir(path) as it:
        for entry in it:
            if entry.is_dir():
                dir_list[entry.name] = entry.path
    return dir_list


def local_list_update(
    dir_list: dict[str, str], plugins: dict[str, dict[str, Any]]
) -> None:
    for name, plugin in plugins.items():
        if name not in dir_list:
            continue

        if plugin.get("manual_local_version", False):
            logger.debug(
                "skip looking version in local txt file, set to manual, plugin: {}",
                plugin,
            )
            continue

        txt = plugin.get("txt", f"{name}.txt")
        txt = f"{dir_list[name]}/{txt}"
        if not os.access(txt, os.R_OK):
            txt = f"{dir_list[name]}/{name}.addon"
        if not os.access(txt, os.R_OK):
            logger.error(f"txt/addon not found or not readable, file: {txt}")
            continue
        # Not sure of proper encoding in plugin files, added "errors=replace" to avoid exception
        txt_content = Path(txt).read_text(errors="replace")
        re_result = re.search("## Version: (.+)", txt_content)
        if re_result is None:
            logger.error(f"plugin version in local txt file not found, file: {txt}")
            continue
        plugin["local_version"] = re_result.group(1)


def get_list_to_remote_check(
    plugins: dict[str, dict[str, Any]], min_interval: int
) -> list[str]:
    to_check = []
    for name, plugin in plugins.items():
        try:
            if "local_version" not in plugin:
                continue
            last_crawl = datetime.fromisoformat(plugin["last_crawl"])
            if last_crawl + timedelta(hours=min_interval) >= datetime.now():
                logger.debug(
                    f"plugin {name} checked less than {min_interval} hours ago, skipping"
                )
                continue
        except KeyError:
            pass
        to_check.append(name)
    return to_check


def remote_version_update(
    plugins: dict[str, dict[str, Any]], to_check: list[str]
) -> None:
    logger.info("checking remote versions")
    for name in to_check:
        print(".", end="", flush=True)
        if "url" not in plugins[name]:
            logger.error(f"missing url for plugin {name}")
            continue
        plugins[name]["last_crawl"] = datetime.now().isoformat()
        page = requests.get(plugins[name]["url"]).text
        soup = BeautifulSoup(page, "html.parser")
        version = soup.find("div", id="version")
        if len(version.contents) != 1:
            logger.error(f"version div not found in crawl result for plugin {name}")
            continue
        re_result = re.search("Version: (.+)", version.string)
        if re_result == None:
            logger.error(f"version regexp in div not found for plugin {name}")
            continue
        plugins[name]["remote_version"] = re_result.group(1)
        download = soup.find("div", id="downloadbutton").find("a")
        if download is None:
            logger.warning(f"could not find download link for plugin: {name}")
            plugins[name]["download_url"] = False
            continue
        try:
            downPage = requests.get("https://www.esoui.com" + download["href"]).text
        except Exception:
            logger.warning(f"could not find download link for plugin: {name}")
            continue
        downSoup = BeautifulSoup(downPage, "html.parser")
        download = downSoup.find("div", class_="manuallink").find("a")
        if download is None:
            logger.warning(f"could not find download link for plugin: {name}")
            plugins[name]["download_url"] = False
            continue
        plugins[name]["download_url"] = download["href"]
    print("")


def get_list_to_update(plugins: dict[str, dict[str, Any]]) -> list[str]:
    to_update = []
    for name, plugin in plugins.items():
        try:
            vremote = plugin["remote_version"]
            vlocal = plugin["local_version"]
        except KeyError:
            logger.error(
                f"cannot check if plugin {name} need update: missing version local/remote"
            )
            continue
        if vremote != vlocal:
            to_update.append(name)
    return to_update


def clean_removed_plugins(
    plugins: dict[str, dict[str, Any]], dir_list: dict[str, str]
) -> None:
    to_delete = []
    for name in plugins:
        if name not in dir_list:
            to_delete.append(name)
    for name in to_delete:
        logger.info(f"plugin {name} dir does not exists anymore, removing from config")
        del plugins[name]


def print_list_to_update(
    to_update: list[str], plugins: dict[str, dict[str, Any]]
) -> None:
    max_len = 0
    for name in to_update:
        max_len = max(len(name), max_len)
    max_len += 1

    print(f"{'Plugin'.ljust(max_len)} {'Local version':<20} {'Remote Version':<20} URL")
    for name in to_update:
        print(
            f"{name.ljust(max_len)} {plugins[name]['local_version'].ljust(20)} {plugins[name]['remote_version'].ljust(20)} {plugins[name]['url']}"
        )


def get_unknown_dirs(
    dir_list: dict[str, str], plugins: dict[str, dict[str, Any]]
) -> list[str]:
    unknown = []
    for name in dir_list:
        if name not in plugins:
            unknown.append(name)
    return unknown


def move_plugins(tmpdir: str, addons_path: str, obsolete_versions_dir: str) -> None:
    obsolete_versions_dir = obsolete_versions_dir + "/" + datetime.now().isoformat()
    with os.scandir(tmpdir) as it:
        for entry in it:
            if entry.is_dir():
                existing_plugin_path = f"{addons_path}/{entry.name}"
                if os.path.exists(existing_plugin_path):
                    shutil.move(
                        existing_plugin_path,
                        f"{obsolete_versions_dir}/{entry.name}",
                    )
                shutil.move(f"{tmpdir}/{entry.name}", f"{addons_path}")


def download_new_versions(
    to_update: list[str], plugins: dict[str, dict[str, Any]], tmpdir: str
) -> None:
    for name in to_update:
        if not plugins[name]["download_url"]:
            continue
        r = requests.get(plugins[name]["download_url"])
        z = zipfile.ZipFile(io.BytesIO(r.content))
        z.extractall(tmpdir)


def main() -> None:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--action",
        help="what to do with plugins to update. list: only list plugins to update in output",
        choices=["list", "update"],
        default="list",
    )
    parser.add_argument(
        "--config_file",
        help="config and data file: path to game, plugins list with versions",
        type=Path,
        default=Path("./config.json"),
    )
    parser.add_argument(
        "--config_backup",
        help="backup config file, in case of issue (tool overwrites it each time), mpty string to disable",
        type=Path,
        default=Path("./config_backup.json"),
    )

    parser.add_argument(
        "--lib_log_level",
        help="log level to use for logs in libraries, value must be a valid python logging level",
        type=str,
        default="warning",
    )
    parser.add_argument(
        "--log_level",
        help="log level, value must be a valid python logging level",
        type=str,
        default="info",
    )
    parser.add_argument(
        "--min_interval",
        help="min number of hours before re-checking remote version since last check",
        type=int,
        default=24,
    )
    parser.add_argument(
        "--max_remotes_check",
        help="max number of plugin to check remote version for (avoid spamming esoui website with too many requests)",
        type=int,
        default=100,
    )

    cfg = parser.parse_args()
    logger.remove()
    logger.add(
        sys.stderr,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> <yellow>{level}</yellow> {message}",
        level=cfg.log_level.upper(),
    )
    logging.basicConfig(
        handlers=[InterceptHandler(level=cfg.lib_log_level.upper())],
        level=0,
        force=True,
    )

    try:
        with open(cfg.config_file) as f:
            config = json.load(f)
            if cfg.config_backup != "":
                shutil.copy(cfg.config_file, cfg.config_backup)
    except FileNotFoundError:
        logger.error(
            "config file not found: {}. Please create it first.", cfg.config_file
        )
        sys.exit(1)

    # Load plugins information file
    try:
        with open(PLUGINS_FILE) as f:
            plugins_data = json.load(f)
    except FileNotFoundError:
        logger.error(
            "plugins file not found: {}. Please ensure it exists.", PLUGINS_FILE
        )
        sys.exit(1)

    # Get plugins data from config (without URLs)
    plugins = config.get("plugins", {})

    # Merge URLs into plugins data for processing
    for plugin_name, plugin_data in plugins.items():
        if plugin_name in plugins_data:
            plugin_data["url"] = plugins_data[plugin_name]["url"]

    dir_list = get_dir_list(config["addons_path"])

    # Add directories that exist in dir_list and plugins_data but are missing from config
    add_missing_plugins_from_list(dir_list, plugins, plugins_data)

    local_list_update(dir_list, plugins)
    clean_removed_plugins(plugins, dir_list)
    to_check = get_list_to_remote_check(plugins, cfg.min_interval)
    unknown = get_unknown_dirs(dir_list, plugins)

    logger.debug("plugins to check remote version for: {}", to_check)
    if cfg.max_remotes_check > 0 and len(to_check) > 0:
        remote_version_update(plugins, to_check[: cfg.max_remotes_check])
    to_update = get_list_to_update(plugins)

    match cfg.action:
        case "list":
            print_list_to_update(to_update, plugins)
            print("Unknown / Ignored dirs:", unknown)
        case "update":
            tmpdir = tempfile.TemporaryDirectory()
            download_new_versions(to_update, plugins, tmpdir.name)
            move_plugins(
                tmpdir.name, config["addons_path"], config["addons_obsolete_path"]
            )

    # Remove URLs from plugins data before saving
    plugins_to_save = {}
    for plugin_name, plugin_data in plugins.items():
        plugin_data_copy = plugin_data.copy()
        if "url" in plugin_data_copy:
            del plugin_data_copy["url"]
        plugins_to_save[plugin_name] = plugin_data_copy

    config["plugins"] = plugins_to_save
    with open(cfg.config_file, "w") as f:
        json.dump(config, f, indent=2)


def add_missing_plugins_from_list(
    dir_list: dict[str, str],
    plugins: dict[str, dict[str, Any]],
    plugins_data: dict[str, dict[str, Any]],
) -> None:
    """Add directories that exist in dir_list and plugins_data but are missing from config plugins."""
    for dir_name in dir_list:
        if dir_name not in plugins and dir_name in plugins_data:
            logger.info(f"Adding plugin {dir_name} from plugins list to config")
            plugins[dir_name] = {"url": plugins_data[dir_name]["url"]}


# Copy/paste from loguru documentation
class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists.
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Find caller from where originated the logged message.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


if __name__ == "__main__":
    main()
