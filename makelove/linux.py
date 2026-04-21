import os
import sys
from urllib.request import urlretrieve, urlopen, URLError
import shutil
import subprocess
import re
import json
from collections import namedtuple
import zipfile

from PIL import Image, UnidentifiedImageError
import appdirs

from .util import fuse_files, tmpfile, parse_love_version, ask_yes_no, download_love_binary
from .config import all_love_versions, should_build_artifact
from .hooks import execute_target_hook


def get_appimagetool_path():
    return os.path.join(appdirs.user_cache_dir("makelove"), "appimagetool")


def download_love_appimage(version):
    """Legacy function - now uses the unified download_love_binary."""
    return download_love_binary(version, "appimage")


def get_release_asset_list(url):
    try:
        with urlopen(url) as req:
            data = json.loads(req.read().decode())
    except Exception as exc:
        sys.exit("Could not retrieve asset list: {}".format(exc))

    return data["assets"]


def get_appimagetool():
    which_appimagetool = shutil.which("appimagetool")
    if which_appimagetool:
        return which_appimagetool
    else:
        appimagetool_path = os.path.join(
            appdirs.user_cache_dir("makelove"), "appimagetool"
        )
        if os.path.isfile(appimagetool_path):
            return appimagetool_path

        url = "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
        try:
            os.makedirs(os.path.dirname(appimagetool_path), exist_ok=True)
            print("Downloading '{}'..".format(url))
            urlretrieve(url, appimagetool_path)
            os.chmod(appimagetool_path, 0o755)
            return appimagetool_path
        except URLError as exc:
            sys.exit("Could not download appimagetool from {}: {}".format(url, exc))



def build_linux(config, version, target, target_directory, love_file_path):
    if os.path.exists(target_directory):
        shutil.rmtree(target_directory)
    os.makedirs(target_directory)

    if target in config and "source_appimage" in config[target]:
        source_appimage = config[target]["source_appimage"]
        print(f"Using manually specified source_appimage: {source_appimage}")
        if not os.path.isabs(source_appimage):
            source_appimage = os.path.join(os.getcwd(), source_appimage)
    else:
        assert "love_version" in config
        print(f"Auto-downloading LÖVE {config['love_version']} AppImage...")
        appimage_cache_dir = download_love_binary(config["love_version"], "appimage")
        source_appimage = os.path.join(appimage_cache_dir, f"love-{config['love_version']}-x86_64.AppImage")

    print("Extracting source AppImage '{}'..".format(source_appimage))
    ret = subprocess.run(
        [source_appimage, "--appimage-extract"],
        cwd=target_directory,
        capture_output=True,
    )
    if ret.returncode != 0:
        sys.exit("Could not extract AppImage: {}".format(ret.stderr.decode("utf-8")))

    appdir_path = os.path.join(target_directory, "squashfs-root")
    appdir = lambda x: os.path.join(appdir_path, x)

    game_name = config["name"]
    if " " in game_name:
        # If stripping is ever removed here, it still needs to be done for the AppImage file name, because of the mentioned bug.
        print(
            "Stripping whitespace from game name.\n"
            "Having spaces in the AppImage filename is problematic. This is a known bug in the AppImage runtime: https://github.com/AppImage/AppImageKit/issues/678\n"
            "Also having spaces in the filename of the fused executable inside the AppImage is problematic, because you can't specify it in the Exec field of the .desktop file.\n"
            "Similarly it leads to problems in the Icon field of the .desktop file.\n"
            "This essay shall justify my lazy attempt to address these problems and motivate you to remove spaces from your game name.\n"
            "It's 2022 at the time of writing this and the technology is just not there, I'm truly sorry."
        )
        game_name = game_name.replace(" ", "")

    # Copy shared_libraries (e.g. luasteam.so, libsteam_api.so) to usr/lib/ inside the AppDir.
    # Native .so files must be on the filesystem — they cannot be loaded from inside the .love zip.
    shared_libraries = []
    if target in config and "shared_libraries" in config[target]:
        shared_libraries.extend(config[target]["shared_libraries"])
    if "linux" in config and "shared_libraries" in config["linux"]:
        shared_libraries.extend(config["linux"]["shared_libraries"])

    if shared_libraries:
        os.makedirs(appdir("usr/lib"), exist_ok=True)
        for lib_path in shared_libraries:
            if os.path.isfile(lib_path):
                print("Copying shared library {} to AppDir usr/lib/".format(lib_path))
                shutil.copy2(lib_path, appdir("usr/lib"))
            else:
                sys.exit("Cannot find shared library: {}".format(lib_path))

    # Place the .love file in the AppDir and fuse or copy as needed
    if os.path.isfile(appdir("usr/bin/wrapper-love")):
        # pfirsich-style AppImages — copy .love file next to the wrapper
        dest_love = appdir("usr/bin/{}.love".format(config["name"]))
        print("Copying {} to {}".format(love_file_path, appdir("usr/bin")))
        shutil.copy2(love_file_path, dest_love)
        desktop_exec = "wrapper-love %F"
    elif os.path.isfile(appdir("bin/love")):
        # Official AppImages (since 11.4) — fuse .love to the love binary
        fused_exe_path = appdir("bin/{}".format(game_name))
        print("Fusing {} and {} into {}".format(appdir("bin/love"), love_file_path, fused_exe_path))
        fuse_files(fused_exe_path, appdir("bin/love"), love_file_path)
        os.chmod(fused_exe_path, 0o755)
        os.remove(appdir("bin/love"))

        # Rename back to bin/love so love.sh can pick it up
        parsed_version = parse_love_version(config["love_version"])
        if (parsed_version[0], parsed_version[1]) >= (11, 4):
            os.rename(fused_exe_path, appdir("bin/love"))

        desktop_exec = "{} %f".format(game_name)
    else:
        sys.exit(
            "Could not find love executable in AppDir. The AppImage has an unknown format."
        )

    # Copy archive_files (e.g. steam_appid.txt) into the AppDir.
    # @content/<name> places the file next to the love binary (bin/).
    # Default (no annotation) places the file in usr/share/.
    archive_files = {}
    if "archive_files" in config:
        archive_files.update(config["archive_files"])
    if "linux" in config and "archive_files" in config["linux"]:
        archive_files.update(config["linux"]["archive_files"])
    if target in config and "archive_files" in config[target]:
        archive_files.update(config[target]["archive_files"])

    for src_path, dest_name in archive_files.items():
        if dest_name.startswith("@content/"):
            dest_file = appdir("bin/{}".format(dest_name[9:]))
        else:
            dest_file = appdir("usr/share/{}".format(dest_name))
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        if os.path.isfile(src_path):
            shutil.copy2(src_path, dest_file)
        elif os.path.isdir(src_path):
            shutil.copytree(src_path, dest_file)
        else:
            sys.exit("Cannot copy archive file '{}'".format(src_path))

    # Copy icon
    icon_file = config.get("icon_file", None)
    if icon_file:
        os.remove(appdir("love.svg"))
        icon_ext = os.path.splitext(icon_file)[1]
        if icon_ext in [".png", ".svg", ".svgz", ".xpm"]:
            dest_icon_path = appdir(game_name + icon_ext)
            print("Copying {} to {}".format(icon_file, dest_icon_path))
            shutil.copy2(icon_file, dest_icon_path)
        else:
            dest_icon_path = appdir(f"{game_name}.png")
            print("Converting {} to {}".format(icon_file, dest_icon_path))
            try:
                img = Image.open(icon_file)
                img.save(dest_icon_path)
            except FileNotFoundError as exc:
                sys.exit("Could not find icon file: {}".format(exc))
            except UnidentifiedImageError as exc:
                sys.exit("Could not read icon file: {}".format(exc))
            except IOError as exc:
                sys.exit("Could not convert icon to .png: {}".format(exc))
    # appimagetool will create a symlink from the icon to .DirIcon
    os.remove(appdir(".DirIcon"))

    # replace love.desktop with [name].desktop
    # https://specifications.freedesktop.org/desktop-entry-spec/desktop-entry-spec-latest.html
    os.remove(appdir("love.desktop"))
    desktop_file_fields = {
        "Type": "Application",
        "Name": config["name"],
        "Exec": desktop_exec,
        "Categories": "Game;",
        "Terminal": "false",
        "Icon": game_name,
    }

    if "linux" in config and "desktop_file_metadata" in config["linux"]:
        desktop_file_fields.update(config["linux"]["desktop_file_metadata"])

    with open(appdir(f"{game_name}.desktop"), "w") as f:
        f.write("[Desktop Entry]\n")
        for k, v in desktop_file_fields.items():
            f.write("{}={}\n".format(k, v))


    # Rebuild AppImage
    if should_build_artifact(config, target, "appimage", True):
        print("Creating new AppImage..")
        appimage_path = os.path.join(target_directory, f"{game_name}.AppImage")
        ret = subprocess.run(
            [get_appimagetool(), appdir_path, appimage_path], capture_output=True
        )
        if ret.returncode != 0:
            sys.exit("Could not create appimage: {}".format(ret.stderr.decode("utf-8")))
        print("Created {}".format(appimage_path))

    if should_build_artifact(config, target, "appdir", False):
        os.rename(appdir_path, os.path.join(target_directory, "AppDir"))
    else:
        print("Removing AppDir..")
        shutil.rmtree(appdir_path)
