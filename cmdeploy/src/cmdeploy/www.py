import hashlib
import re
import shutil
import time
import traceback
import webbrowser
from pathlib import Path

import markdown
from chatmaild.config import read_config
from jinja2 import Template

from .genqr import gen_qr_png_data

_MERGE_CONFLICT_RE = re.compile(
    r"^<<<<<<<.+^=======.+^>>>>>>>", re.DOTALL | re.MULTILINE
)
_SKIP_DIR_NAMES = {"node_modules", "qr-src", ".git", "__pycache__"}
_SKIP_FILE_NAMES = {
    ".gitignore",
    "LICENSE",
    "README.md",
    "mocha-start.js",
    "package.json",
    "package-lock.json",
    "playwright.config.js",
}


def snapshot_dir_stats(somedir):
    d = {}
    for path in somedir.iterdir():
        if path.is_file() and path.name[0] != "." and path.suffix != ".swp":
            mtime = path.stat().st_mtime
            hash = hashlib.md5(path.read_bytes()).hexdigest()
            d[path] = (mtime, hash)
    return d


def prepare_template(source):
    assert source.exists(), source
    render_vars = {}
    render_vars["pagename"] = "home" if source.stem == "index" else source.stem
    render_vars["markdown_html"] = markdown.markdown(source.read_text())
    page_layout = source.with_name("page-layout.html").read_text()
    return render_vars, page_layout


def get_paths(config) -> (Path, Path, Path):
    reporoot = (Path(__file__).resolve() / "../../../../").resolve()
    www_path = Path(config.www_folder)
    # if www_folder was not set, use default directory
    if config.www_folder == "":
        www_path = reporoot.joinpath("www")
    src_dir = www_path.joinpath("src")
    # Build if markdown or ready-made html homepage is present.
    if src_dir.joinpath("index.md").is_file() or src_dir.joinpath("index.html").is_file():
        build_dir = www_path.joinpath("build")
    # if it is not a hugo page, upload it as is
    else:
        build_dir = None
    return www_path, src_dir, build_dir


def build_webpages(src_dir, build_dir, config) -> Path:
    try:
        return _build_webpages(src_dir, build_dir, config)
    except Exception:
        print(traceback.format_exc())


def int_to_english(number):
    if number >= 0 and number <= 12:
        a = [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        ]
        return a[number]
    elif number <= 50:
        return str(number)
    if number > 50:
        return "more"


def _build_webpages(src_dir, build_dir, config):
    mail_domain = config.mail_domain
    assert src_dir.exists(), src_dir
    build_dir.mkdir(parents=True, exist_ok=True)
    for path in build_dir.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    qr_path = build_dir.joinpath(f"qr-chatmail-invite-{mail_domain}.png")
    qr_path.write_bytes(gen_qr_png_data(mail_domain).read())

    explicit_html_names = {
        path.name for path in src_dir.iterdir() if path.is_file() and path.suffix == ".html"
    }

    for path in src_dir.iterdir():
        if not _should_copy_path(path):
            continue
        if path.suffix == ".md":
            render_vars, content = prepare_template(path)
            render_vars["username_min_length"] = int_to_english(
                config.username_min_length
            )
            render_vars["username_max_length"] = int_to_english(
                config.username_max_length
            )
            render_vars["password_min_length"] = int_to_english(
                config.password_min_length
            )
            target = build_dir.joinpath(path.stem + ".html")
            if target.name in explicit_html_names:
                continue

            # recursive jinja2 rendering
            while 1:
                new = Template(content).render(config=config, **render_vars)
                if new == content:
                    break
                content = new

            with target.open("w") as f:
                f.write(content)
        elif path.name != "page-layout.html":
            target = build_dir.joinpath(path.name)
            if path.is_dir():
                _copy_dir(path, target)
            else:
                target.write_bytes(path.read_bytes())
    return build_dir


def _copy_dir(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if not _should_copy_path(child):
            continue
        if child.is_dir():
            _copy_dir(child, dst.joinpath(child.name))
        else:
            dst.joinpath(child.name).write_bytes(child.read_bytes())


def _should_copy_path(path: Path) -> bool:
    if path.is_dir():
        return path.name not in _SKIP_DIR_NAMES
    if path.name in _SKIP_FILE_NAMES:
        return False
    if path.name.startswith("ui-test-"):
        return False
    return True


def find_merge_conflict(src_dir) -> Path:
    assert src_dir.exists(), src_dir
    result = None
    for path in src_dir.iterdir():
        if path.suffix in [".css", ".html", ".md"]:
            if _MERGE_CONFLICT_RE.search(path.read_text()):
                result = path
                break
    return result


def main():
    reporoot = (Path(__file__).resolve() / "../../../../").resolve()
    inipath = reporoot.joinpath("chatmail.ini")
    config = read_config(inipath)
    config.webdev = True
    assert config.mail_domain

    www_path, src_path, build_dir = get_paths(config)
    build_dir = build_webpages(src_path, build_dir, config)
    index_path = build_dir.joinpath("index.html")
    webbrowser.open(str(index_path))

    print(f"\nOpened URL: file://{index_path.resolve()}\n")
    print(f"Watching {src_path} directory for changes...")

    stats = snapshot_dir_stats(src_path)
    changenum = 0
    debounce_time = 0.5  # wait 0.5s after detecting a change

    while True:
        time.sleep(1)
        newstats = snapshot_dir_stats(src_path)

        if newstats != stats:
            changed_files = [f for f in newstats if stats.get(f) != newstats[f]]
            for f in changed_files:
                print(f"*** CHANGED: {f}")

            stats = newstats
            changenum += 1
            build_webpages(src_path, build_dir, config)
            print(f"[{changenum}] regenerated web pages at: {index_path}")
            print(f"URL: file://{index_path.resolve()}\n\n")

            time.sleep(debounce_time)  # simple debounce


if __name__ == "__main__":
    main()
