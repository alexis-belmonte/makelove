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


def extract_native_modules_from_love(love_file_path, target_dir):
    """Extract native Lua modules (.so files) from .love archive to target directory.
    
    This is necessary because when LÖVE runs with an embedded game, its module loader
    cannot find native C modules inside the embedded .love file. They need to be in
    a location that's on package.path, like /lib/ inside the AppImage.
    """
    extracted = []
    try:
        with zipfile.ZipFile(love_file_path, 'r') as love_zip:
            for name in love_zip.namelist():
                # Look for .so files anywhere in the .love archive
                if name.endswith('.so'):
                    # Read bytes explicitly to avoid any corruption
                    data = love_zip.read(name)
                    # Extract to target_dir with just the filename (flatten structure)
                    basename = os.path.basename(name)
                    output_path = os.path.join(target_dir, basename)
                    with open(output_path, 'wb') as f:
                        f.write(data)
                    if basename not in extracted:
                        extracted.append(basename)
    except Exception as e:
        print(f"Warning: Could not extract native modules from {love_file_path}: {e}")
    return extracted


def build_linux(config, version, target, target_directory, love_file_path):
    # Auto-download LÖVE AppImage based on love_version
    if target in config and "source_appimage" in config[target]:
        # Manual override still supported for advanced users
        source_appimage = config[target]["source_appimage"]
        print(f"Using manually specified source_appimage: {source_appimage}")
        
        # We need to set the cwd to our extraction destination, so we need to turn this into an absolute path
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
    appdirbin_path = os.path.join(appdir_path, "bin")
    appdirbin = lambda x: os.path.join(appdirbin_path, x)

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

    # Copy .love into AppDir
    if os.path.isfile(appdir("usr/bin/wrapper-love")):
        # pfirsich-style AppImages - > simply copy the love file into the image
        print("Copying {} to {}".format(love_file_path, appdir("usr/bin")))
        shutil.copy2(love_file_path, appdir("usr/bin"))
        desktop_exec = "wrapper-love %F"
    elif os.path.isfile(appdir("bin/love")):
        # Official AppImages (since 11.4) -> fuse the .love file to the love binary
        fused_exe_path = appdir(f"bin/{game_name}")
        print(
            "Fusing {} and {} into {}".format(
                appdir("bin/love"), love_file_path, fused_exe_path
            )
        )
        fuse_files(fused_exe_path, appdir("bin/love"), love_file_path)
        os.chmod(fused_exe_path, 0o755)
        os.remove(appdir("bin/love"))

        # rename back to bin/love so love.sh can pick it up
        parsed_version = parse_love_version(config["love_version"])
        if (parsed_version[0], parsed_version[1]) >= (11, 4):
            os.rename(fused_exe_path, appdir("bin/love"))

        desktop_exec = f"{game_name} %f"
    else:
        sys.exit(
            "Could not find love executable in AppDir. The AppImage has an unknown format."
        )

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
        "Icon": "love",
    }
    if icon_file:
        desktop_file_fields["Icon"] = game_name

    if "linux" in config and "desktop_file_metadata" in config["linux"]:
        desktop_file_fields.update(config["linux"]["desktop_file_metadata"])

    with open(appdir(f"{game_name}.desktop"), "w") as f:
        f.write("[Desktop Entry]\n")
        for k, v in desktop_file_fields.items():
            f.write("{}={}\n".format(k, v))

    # archive files with path annotations
    archive_files = {}
    if "archive_files" in config:
        archive_files.update(config["archive_files"])
    if target in config and "archive_files" in config[target]:
        archive_files.update(config[target]["archive_files"])
    if "linux" in config and "archive_files" in config["linux"]:
        archive_files.update(config["linux"]["archive_files"])

    for k, v in archive_files.items():
        # Handle path annotations
        if v.startswith("@content/"):
            # Files go to squashfs-root/ (inner AppImage content root)
            path = appdir(v[9:])  # Remove "@content/" prefix
        elif v.startswith("@root/"):
            # Files go to export root (same level as .AppImage)
            path = os.path.join(target_directory, v[6:])  # Remove "@root/" prefix
        else:
            # Default: files go to bin/ directory
            path = appdirbin(v)
        
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(k):
            shutil.copyfile(k, path)
        elif os.path.isdir(k):
            shutil.copytree(k, path)
        else:
            sys.exit("Cannot copy archive file '{}'".format(k))

    # Determine the library directory for native modules
    if os.path.isfile(appdir("usr/lib/liblove.so")):
        # pfirsich-style AppImages
        so_target_dir = appdir("usr/lib")
    elif os.path.isfile(appdir("lib/liblove.so")):
        # Official AppImages (since 11.4)
        so_target_dir = appdir("lib/")
    elif os.path.isfile(appdir("lib/liblove-{}.so".format(config["love_version"]))):
        # Support for >= 11.5
        so_target_dir = appdir("lib/")
    else:
        sys.exit(
            "Could not find liblove.so in AppDir. The AppImage has an unknown format."
        )

    # Shared libraries from config
    if target in config and "shared_libraries" in config[target]:
        for f in config[target]["shared_libraries"]:
            shutil.copy(f, so_target_dir)

    # Extract native Lua modules from .love file to bin/ (same dir as executable)
    # This is necessary because embedded games' native modules aren't found by package.path
    extracted_modules = extract_native_modules_from_love(love_file_path, appdirbin(""))
    if extracted_modules:
        print(f"Extracted native modules to {appdirbin('')}: {', '.join(extracted_modules)}")

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
