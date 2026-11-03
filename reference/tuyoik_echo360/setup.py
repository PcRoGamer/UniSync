import setuptools

with open("README.md", "r", encoding="utf8") as fh:
    long_description = fh.read()

with open("requirements.txt") as f:
    required = f.read().splitlines()

setuptools.setup(
    name="echo360-dlp",
    version="3.0.0",
    description="Download Echo360 / Echo360 Cloud lecture videos and VTT transcripts (fork of soraxas/echo360)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/tuyoik/echo360-dlp",
    license="MIT",
    author="Andrew Zhu",
    maintainer="Andrew Zhu",
    project_urls={
        "Homepage": "https://github.com/tuyoik/echo360-dlp",
        "Quick Start": "https://github.com/tuyoik/echo360-dlp/blob/master/QUICKSTART.md",
        "Upstream": "https://github.com/soraxas/echo360",
        "Documentation": "https://github.com/tuyoik/echo360-dlp/blob/master/USAGE.md",
        "Issue tracker": "https://github.com/tuyoik/echo360-dlp/issues",
    },
    python_requires=">=2.7",
    install_requires=required,
    platforms="linux, macos, windows",
    entry_points={
        "console_scripts": [
            "echo360-dlp=echo360.main:main",
            "echo360-downloader=echo360.main:main",
        ],
    },
    package_data={"echo360": ["*.py"]},
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
        "Programming Language :: Unix Shell",
        "Topic :: System :: Shells",
        "Topic :: System :: System Shells",
        "Topic :: Terminals",
        "Topic :: System :: Networking",
        "License :: OSI Approved :: MIT License",
    ],
)
