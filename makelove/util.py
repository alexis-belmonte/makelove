import sys
import tempfile
import atexit
import os
import re
import shutil
from urllib.request import urlretrieve, urlopen, URLError
import zipfile

import appdirs


def eprint(*args, **kwargs):
    print(*args, kwargs, file=sys.stderr)


def _tempfile_deleter(path):
    if os.path.isfile(path):
        os.remove(path)


def tmpfile(*args, **kwargs):
    (fd, path) = tempfile.mkstemp(*args, **kwargs)
    os.close(fd)
    atexit.register(_tempfile_deleter, path)
    return path


def parse_love_version(version_str):
    parts = list(map(int, re.split(r"_|\.", version_str)))
    if len(parts) == 3 and parts[0] == 0:
        parts = parts[1:]
    if len(parts) != 2:
        sys.exit("Could not parse version '{}'".format(".".join(parts)))
    return parts


def strtobool(value):
    """Convert a string representation of truth to true (1) or false (0).
    True values are y, yes, t, true, on and 1; false values are n, no, f, false, off and 0. Raises ValueError if val is anything else.
    """
    l = value.lower()
    if l in ("y", "yes", "t", "true", "on", "1"):
        return 1
    elif l in ("n", "no", "f", "false", "off", "0"):
        return 0
    else:
        raise ValueError("invalid truth value %r" % (value,))


def ask_yes_no(question, default=None):
    if default == None:
        option_str = "[y/n]: "
    else:
        option_str = " [{}/{}]: ".format(
            "Y" if default else "y", "N" if not default else "n"
        )

    while True:
        sys.stdout.write(question + option_str)
        choice = input().lower()
        if choice == "" and default != None:
            return default
        else:
            try:
                return bool(strtobool(choice))
            except ValueError:
                sys.stdout.write("Invalid answer.\n")


def prompt(prompt_str, default=None):
    default_str = ""
    if default != None:
        default_str = " [{}]".format(default)
    while True:
        sys.stdout.write(prompt_str + default_str + ": ")
        s = input()
        if s:
            return s
        else:
            if default != None:
                return default


def get_love_cache_dir():
    """Get the cache directory for LÖVE binaries."""
    return os.path.join(appdirs.user_cache_dir("makelove"), "love")


def get_love_binary_cache_path(version, platform):
    """Get the cache path for a specific LÖVE version and platform."""
    return os.path.join(get_love_cache_dir(), version, platform)


def download_love_binary(version, platform):
    """
    Download LÖVE binaries for the specified version and platform.
    Returns the path to the downloaded/extracted binaries.
    
    Platforms: win32, win64, macos, appimage
    """
    cache_path = get_love_binary_cache_path(version, platform)
    
    # Check if already cached
    if os.path.isdir(cache_path) and _verify_love_cache(cache_path, platform):
        print(f"Using cached LÖVE {version} for {platform}")
        return cache_path
    
    print(f"Downloading LÖVE {version} for {platform}...")
    
    try:
        # Download the binary
        download_url, filename = _get_love_download_info(version, platform)
        temp_file = tmpfile(suffix=".zip" if filename.endswith(".zip") else ".AppImage")
        
        print(f"  Downloading from: {download_url}")
        urlretrieve(download_url, temp_file)
        
        # Create cache directory
        os.makedirs(cache_path, exist_ok=True)
        
        # Extract/download to cache
        if platform == "appimage":
            # AppImage is already a single file, just move it
            dest_path = os.path.join(cache_path, f"love-{version}-x86_64.AppImage")
            shutil.move(temp_file, dest_path)
        else:
            # Extract zip file
            with zipfile.ZipFile(temp_file, "r") as zip_ref:
                zip_ref.extractall(cache_path)
            
            # Handle macOS special case (zip contains love.zip)
            if platform == "macos":
                macos_zip = os.path.join(cache_path, "love.zip")
                if os.path.isfile(macos_zip):
                    # Extract the inner zip
                    temp_extract = tempfile.mkdtemp()
                    with zipfile.ZipFile(macos_zip, "r") as inner_zip:
                        inner_zip.extractall(temp_extract)
                    # Move contents to cache_path
                    for item in os.listdir(temp_extract):
                        shutil.move(os.path.join(temp_extract, item), cache_path)
                    shutil.rmtree(temp_extract)
                    os.remove(macos_zip)
        
        print(f"  Cached at: {cache_path}")
        return cache_path
        
    except Exception as e:
        eprint(f"Error downloading LÖVE {version} for {platform}: {e}")
        sys.exit(1)
    finally:
        # Clean up temp file if it still exists
        if os.path.isfile(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass


def _get_love_download_info(version, platform):
    """
    Get the download URL and filename for a specific LÖVE version and platform.
    Returns (url, filename) tuple.
    """
    base_url = f"https://github.com/love2d/love/releases/download/{version}"
    
    parsed_version = parse_love_version(version)
    
    if platform == "appimage":
        # Linux AppImage
        url = f"{base_url}/love-{version}-x86_64.AppImage"
        filename = f"love-{version}-x86_64.AppImage"
        return url, filename
    
    elif platform == "macos":
        # macOS
        if parsed_version[0] <= 8:
            platform_name = "macosx-ub"
        elif parsed_version[0] in (9, 10):
            platform_name = "macosx-x64"
        else:
            platform_name = "macos"
        
        # Handle version format
        version_str = version if version != "11.0" else "11.0.0"
        url = f"{base_url}/love-{version_str}-{platform_name}.zip"
        filename = f"love-{version_str}-{platform_name}.zip"
        return url, filename
    
    elif platform in ("win32", "win64"):
        # Windows
        arch = "x86" if platform == "win32" else "x64"
        
        if parsed_version[0] <= 8:
            platform_name = f"win-{arch}"
        else:
            platform_name = arch
        
        # Handle version format
        version_str = version if version != "11.0" else "11.0.0"
        url = f"{base_url}/love-{version_str}-{platform_name}.zip"
        filename = f"love-{version_str}-{platform_name}.zip"
        return url, filename
    
    else:
        sys.exit(f"Unknown platform: {platform}")


def _verify_love_cache(cache_path, platform):
    """Verify that the cached binaries are valid."""
    if not os.path.isdir(cache_path):
        return False
    
    if platform == "appimage":
        appimage_path = os.path.join(cache_path, "love-*.AppImage")
        import glob
        return len(glob.glob(appimage_path)) > 0
    elif platform == "macos":
        # macOS cache should contain love.zip or extracted contents
        return os.path.isfile(os.path.join(cache_path, "love.zip")) or                os.path.isdir(os.path.join(cache_path, "love.app"))
    else:
        # Windows - check for .exe file
        exe_exists = any(f.endswith(".exe") for f in os.listdir(cache_path))
        return exe_exists


def get_default_love_binary_dir(version, platform):
    """Backward compatibility function."""
    return get_love_binary_cache_path(version, platform)


def get_download_url(version, platform):
    """Backward compatibility function - returns just the URL."""
    url, _ = _get_love_download_info(version, platform)
    return url


def fuse_files(dest_path, *src_paths):
    with open(dest_path, "wb") as fused:
        for path in src_paths:
            with open(path, "rb") as f:
                fused.write(f.read())
