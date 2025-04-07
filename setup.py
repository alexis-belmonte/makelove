import setuptools
import re

VERSION_FILE = "makelove/_version.py"
with open(VERSION_FILE, "rt") as vf:
    version_line = vf.read()
    version_match = re.search(r"__version__ = ['\"]([^'\"]*)['\"]", version_line)
    if version_match:
        version_string = version_match.group(1)
    else:
        raise RuntimeError(f"Unable to parse version string in {VERSION_FILE}")


with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="makelove",
    version=version_string,
    author="Joel Schumacher",
    author_email="joelschum@gmail.com",
    description="A packaging tool for [löve](https://love2d.org) games",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/pfirsich/makelove",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.7",
    install_requires=["Pillow>=7.0", "appdirs>=1.4.3", "toml>=0.10"],
    entry_points={
        "console_scripts": ["makelove=makelove.makelove:main"],
    },
)
