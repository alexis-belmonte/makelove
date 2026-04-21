import io
import os
import plistlib
import struct
import sys
import shutil
from pathlib import Path
from datetime import datetime
from zipfile import ZipFile
from urllib.request import urlopen, urlretrieve, URLError

from PIL import Image

from .util import eprint, get_default_love_binary_dir, get_download_url, download_love_binary, get_love_binary_cache_path
from .hooks import execute_target_hook


def write_file(pkg, name, content):
    if isinstance(pkg, str):
        mode = "w" if isinstance(content, str) else "wb"
        with open(name, mode) as f:
            f.write(content)
    elif isinstance(pkg, ZipFile):
        pkg.writestr(name, content)


def make_icns(iconfile, icon_image_file):
    """
    iconfile: an open file to write the ICNS file contents into (mode: wb)
    icon_image: a PIL.Image object of the icon image

    Based on code from learn-python.com:
      https://learning-python.com/cgi/showcode.py?name=pymailgui-products/unzipped/build/build-icons/iconify.py
    """
    icon_image = Image.open(icon_image_file)

    # must all be square (width=height) and of standard pixel sizes
    width, height = icon_image.size  # a 2-tuple
    if width != height:
        eprint("Invalid image size, discarded: %d x %d." % (width, height))
        sys.exit(1)

    sizetotypes = {
        16: [b"icp4"],  # 16x16   std only  (no 8x8@2x)
        32: [b"icp5", b"ic11"],  # 32x32   std -AND- 16x16@2x   high
        64: [b"icp6", b"ic12"],  # 64x64   std -AND- 32x32@2x   high
        128: [b"ic07"],  # 128x128 std only  (no 64x64@2x)
        256: [b"ic08", b"ic13"],  # 256x256 std -AND- 128x128@2x high
        512: [b"ic09", b"ic14"],  # 512x512 std -AND- 256x256@2x high
        1024: [b"ic10"],  # 1024x1024 (10.7) = 512x512@2x high (10.8)
    }

    imagedatas = []
    for size_px, icontypes in sizetotypes.items():
        img = icon_image.resize((size_px, size_px), Image.LANCZOS)
        with io.BytesIO() as img_data_f:
            img.save(img_data_f, "png")
            for icontype in icontypes:
                imagedatas.append([icontype, img_data_f.getvalue()])

    # 1) HEADER: 4-byte "magic" + 4-byte filesize (including header itself)

    filelen = 8 + sum(len(imagedata) + 8 for (_, imagedata) in sorted(imagedatas))
    iconfile.write(b"icns")
    iconfile.write(struct.pack(">I", filelen))

    # 2) IMAGE TYPE+LENGTH+BYTES: packed into rest of icon file sequentially

    for icontype, imagedata in imagedatas:
        # data length includes type and length fields (4+4)
        iconfile.write(icontype)  # 4 byte type
        iconfile.write(struct.pack(">I", 8 + len(imagedata)))  # 4-byte length
        iconfile.write(imagedata)  # and the image


def get_game_icon_content(config):
    # Mac icons are not supposed to take up the full image area and generally
    # have shadows, etc - allow users to provide a different design but fall
    # back on the generic icon_file setting
    icon_file = config.get("macos", {}).get("icon_file")
    if icon_file is None:
        icon_file = config.get("icon_file", None)
    elif not os.path.isfile(icon_file):
        sys.exit(f"Couldn't find macOS icon_file at {icon_file}")

    if icon_file is None:
        icon_file = config.get("icon_file", None)
    elif not os.path.isfile(icon_file):
        sys.exit(f"Couldn't find icon_file at {icon_file}")

    if not icon_file:
        return False

    with io.BytesIO() as icns_f, open(icon_file, "rb") as icon_img_f:
        icon_key = f"{config['name']}.app/Contents/Resources/icon-{config['name']}.icns"
        if icon_file.lower().endswith(".png"):
            make_icns(icns_f, icon_img_f)
            return icns_f.getvalue()
        else:
            return icon_img_f.read()


def get_info_plist_content(config, version):
    plist = {
        "BuildMachineOSBuild": "19B88",
        "CFBundleDevelopmentRegion": "English",
        "CFBundleExecutable": "love",
        "CFBundleIconFile": "icon.icns",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "LoVe",
        "CFBundleSupportedPlatforms": ["MacOSX"],
        "DTCompiler": "com.apple.compilers.llvm.clang.1_0",
        "DTPlatformBuild": "11C504",
        "DTPlatformVersion": "GM",
        "DTSDKBuild": "19B90",
        "DTSDKName": "macosx10.15",
        "DTXcode": "1130",
        "DTXcodeBuild": "11C504",
        "LSApplicationCategoryType": "public.app-category.games",
        "LSMinimumSystemVersion": "10.7",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSSupportsAutomaticGraphicsSwitching": False,
        # dynamic defaults
        "CFBundleShortVersionString": version or config["love_version"],
        "CFBundleName": config["name"],
        "NSHumanReadableCopyright": "© 2006-2020 LÖVE Development Team",
        "CFBundleIdentifier": f"tld.yourgamename",
    }

    if "macos" in config and "app_metadata" in config["macos"]:
        metadata = config["macos"]["app_metadata"]
        plist.update(metadata)

    return plistlib.dumps(plist)


def build_macos(config, version, target, target_directory, love_file_path):
    if os.path.exists(target_directory):
        shutil.rmtree(target_directory)
    os.makedirs(target_directory)

    if target in config and "love_binaries" in config[target]:
        love_binaries = config[target]["love_binaries"]
        print(f"Using manually specified love_binaries: {love_binaries}")
    else:
        assert "love_version" in config
        print(f"Auto-downloading LÖVE {config['love_version']} for macos...")
        love_binaries = download_love_binary(config["love_version"], "macos")

    love_zip_path = os.path.join(love_binaries, "love.zip")
    if not os.path.isfile(love_zip_path):
        cache_dir = get_love_binary_cache_path(config["love_version"], "macos")
        sys.exit(
            "Could not find love.zip in {}.\n"
            "If you have an outdated cache, delete {} and retry.".format(
                love_binaries, cache_dir
            )
        )

    dst = os.path.join(target_directory, "{}-{}.zip".format(config["name"], target))
    with open(love_zip_path, "rb") as lovef, ZipFile(lovef) as love_binary_zip, \
         open(dst, "wb+") as outf, ZipFile(outf, mode="w") as app_zip, \
         open(love_file_path, "rb") as love_zip_f:

        archive_files = {}
        if "archive_files" in config:
            archive_files.update(config["archive_files"])
        if "macos" in config and "archive_files" in config["macos"]:
            archive_files.update(config["macos"]["archive_files"])

        written_archive_files = set()
        root_files = {}

        for src_path, dest_path in archive_files.items():
            if dest_path.startswith("@content/"):
                # Files go to Contents/MacOS/ (next to the love executable)
                path = "{}.app/Contents/MacOS/{}".format(config["name"], dest_path[9:])
            elif dest_path.startswith("@root/"):
                # Files go to the zip root (same level as the .app bundle)
                filename = dest_path[6:]
                if os.path.isfile(src_path):
                    with open(src_path, "rb") as file:
                        root_files[filename] = file.read()
                elif os.path.isdir(src_path):
                    directory = Path(src_path)
                    for file_path in directory.glob("**/*"):
                        if not file_path.is_file():
                            continue
                        with open(file_path, "rb") as file:
                            relative = file_path.relative_to(src_path)
                            root_files["{}/{}".format(filename, relative)] = file.read()
                else:
                    sys.exit("Cannot copy archive file '{}'".format(src_path))
                continue
            else:
                # Default: files go to Contents/Resources/
                path = "{}.app/Contents/Resources/{}".format(config["name"], dest_path)

            if os.path.isfile(src_path):
                with open(src_path, "rb") as file:
                    app_zip.writestr(path, file.read())
            elif os.path.isdir(src_path):
                directory = Path(src_path)
                for file_path in directory.glob("**/*"):
                    if not file_path.is_file():
                        continue
                    with open(file_path, "rb") as file:
                        relative = file_path.relative_to(src_path)
                        app_zip.writestr("{}/{}".format(path, relative), file.read())
            else:
                sys.exit("Cannot copy archive file '{}'".format(src_path))
            written_archive_files.add(path)

        for zipinfo in love_binary_zip.infolist():
            if not zipinfo.filename.startswith("love.app/"):
                eprint("Got bad or unexpectedly formatted love zip file")
                sys.exit(1)

            orig_filename = zipinfo.filename

            # Rename app from "love.app" to the game name
            zipinfo.filename = config["name"] + zipinfo.filename[len("love"):]

            zipinfo.date_time = tuple(datetime.now().timetuple()[:6])

            if zipinfo.filename in written_archive_files:
                continue
            elif orig_filename == "love.app/Contents/Resources/GameIcon.icns":
                continue
            elif orig_filename == "love.app/Contents/Resources/Assets.car":
                continue
            elif orig_filename == "love.app/Contents/Resources/OS X AppIcon.icns":
                zipinfo = "{}.app/Contents/Resources/icon.icns".format(config["name"])
                content = get_game_icon_content(config)
                if not content:
                    content = love_binary_zip.read(orig_filename)
            elif orig_filename == "love.app/Contents/Info.plist":
                app_zip.writestr(zipinfo.filename, get_info_plist_content(config, version))
                continue
            else:
                content = love_binary_zip.read(orig_filename)

            app_zip.writestr(zipinfo, content)

        # Place the pre-built .love in Contents/Resources/
        love_zip_key = "{}.app/Contents/Resources/{}.love".format(config["name"], config["name"])
        app_zip.writestr(love_zip_key, love_zip_f.read())

        for filename, content in root_files.items():
            app_zip.writestr(filename, content)

    if target in config and "artifacts" in config[target] and "directory" in config[target]["artifacts"]:
        unzip_dst = os.path.join(target_directory, "{}-{}".format(config["name"], target))
        with ZipFile(dst, "r") as zip_ref:
            zip_ref.extractall(unzip_dst)
        os.remove(dst)
    
