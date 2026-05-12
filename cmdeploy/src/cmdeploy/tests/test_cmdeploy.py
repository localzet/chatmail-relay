import importlib
import os

import pytest

from cmdeploy.cmdeploy import get_parser, main, update_chatmail_control_config
from cmdeploy.www import get_paths


@pytest.fixture(autouse=True)
def _chdir(tmp_path):
    old = os.getcwd()
    os.chdir(tmp_path)
    yield
    os.chdir(old)


class TestCmdline:
    def test_parser(self, capsys):
        parser = get_parser()
        parser.parse_args([])
        init = parser.parse_args(["init", "chat.example.org"])
        run = parser.parse_args(["run"])
        adm = parser.parse_args(["adm"])
        assert init and run and adm
        assert run.ssh_host == "localhost"

    def test_init_not_overwrite(self, capsys, tmp_path, monkeypatch):
        monkeypatch.delenv("CHATMAIL_INI", raising=False)
        inipath = tmp_path / "chatmail.ini"
        args = ["init", "--config", str(inipath), "chat.example.org"]
        assert main(args) == 0
        capsys.readouterr()

        assert main(args) == 1
        out, err = capsys.readouterr()
        assert "path exists" in out.lower()

        args.insert(1, "--force")
        assert main(args) == 0
        out, err = capsys.readouterr()
        assert "deleting config file" in out.lower()

    def test_update_chatmail_control_config(self, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            "\n".join(
                [
                    "[server]",
                    'public_url = "https://admin.example.com"',
                    "secure_cookies = false",
                    "",
                    "[auth]",
                    'session_secret = "CHANGE_ME_64_RANDOM_CHARS"',
                    "",
                    "[health]",
                    'domain = "example.com"',
                    "",
                ]
            )
        )

        update_chatmail_control_config(
            config,
            public_url="https://admin.chat.example.org",
            mail_domain="chat.example.org",
        )

        text = config.read_text()
        assert 'public_url = "https://admin.chat.example.org"' in text
        assert "secure_cookies = true" in text
        assert 'domain = "chat.example.org"' in text
        assert "CHANGE_ME_64_RANDOM_CHARS" not in text


def test_www_folder(example_config, tmp_path):
    reporoot = importlib.resources.files(__package__).joinpath("../../../../").resolve()
    assert not example_config.www_folder
    www_path, src_dir, build_dir = get_paths(example_config)
    assert www_path.absolute() == reporoot.joinpath("www").absolute()
    assert src_dir == reporoot.joinpath("www").joinpath("src")
    assert build_dir == reporoot.joinpath("www").joinpath("build")
    example_config.www_folder = "disabled"
    www_path, _, _ = get_paths(example_config)
    assert not www_path.is_dir()
    example_config.www_folder = str(tmp_path)
    www_path, src_dir, build_dir = get_paths(example_config)
    assert www_path == tmp_path
    assert not src_dir.exists()
    assert not build_dir
    src_path = tmp_path.joinpath("src")
    os.mkdir(src_path)
    with open(src_path / "index.md", "w") as f:
        f.write("# Test")
    www_path, src_dir, build_dir = get_paths(example_config)
    assert www_path == tmp_path
    assert src_dir == src_path
    assert build_dir == tmp_path.joinpath("build")
