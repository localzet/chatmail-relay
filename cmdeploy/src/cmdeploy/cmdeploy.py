"""
Provides the `cmdeploy` entry point function,
along with command line option and subcommand parsing.
"""

import argparse
import importlib.resources
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pyinfra
from chatmaild.config import read_config, write_initial_config
from packaging import version
from termcolor import colored

from . import dns, remote
from .sshexec import LocalExec, SSHExec

#
# cmdeploy sub commands and options
#


def init_cmd_options(parser):
    parser.add_argument(
        "chatmail_domain",
        action="store",
        help="fully qualified DNS domain name for your chatmail instance",
    )
    parser.add_argument(
        "--force",
        dest="recreate_ini",
        action="store_true",
        help="force reacreate ini file",
    )


def init_cmd(args, out):
    """Initialize chatmail config file."""
    mail_domain = args.chatmail_domain
    inipath = args.inipath
    if args.inipath.exists():
        if not args.recreate_ini:
            print(f"[WARNING] Path exists, not modifying: {inipath}")
            return 1
        else:
            print(
                f"[WARNING] Force argument was provided, deleting config file: {inipath}"
            )
            inipath.unlink()

    write_initial_config(inipath, mail_domain, overrides={})
    out.green(f"created config file for {mail_domain} in {inipath}")


def run_cmd_options(parser):
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="don't actually modify the server",
    )
    parser.add_argument(
        "--disable-mail",
        dest="disable_mail",
        action="store_true",
        help="install/upgrade the server, but disable postfix & dovecot for now",
    )
    parser.add_argument(
        "--website-only",
        action="store_true",
        help="only update/deploy the website, skipping full server upgrade/deployment, useful when you only changed/updated the web pages and don't need to re-run a full server upgrade",
    )
    parser.add_argument(
        "--skip-dns-check",
        dest="dns_check_disabled",
        action="store_true",
        help="disable checks nslookup for dns",
    )
    add_ssh_host_option(parser)


def run_cmd(args, out):
    """Deploy chatmail services on the remote server."""

    ssh_host = args.ssh_host
    sshexec = get_sshexec(ssh_host)
    require_iroh = args.config.enable_iroh_relay
    strict_tls = args.config.tls_cert_mode == "acme"
    if not args.dns_check_disabled:
        remote_data = dns.get_initial_remote_data(sshexec, args.config.mail_domain)
        if not dns.check_initial_remote_data(remote_data, strict_tls=strict_tls, print=out.red):
            return 1

    env = os.environ.copy()
    env["CHATMAIL_INI"] = args.inipath
    env["CHATMAIL_WEBSITE_ONLY"] = "True" if args.website_only else ""
    env["CHATMAIL_DISABLE_MAIL"] = "True" if args.disable_mail else ""
    env["CHATMAIL_REQUIRE_IROH"] = "True" if require_iroh else ""
    deploy_path = importlib.resources.files(__package__).joinpath("run.py").resolve()
    pyinf = "pyinfra --dry" if args.dry_run else "pyinfra"

    cmd = f"{pyinf} --ssh-user root {ssh_host} {deploy_path} -y"
    if ssh_host == "localhost":
        cmd = f"{pyinf} @local {deploy_path} -y"

    if version.parse(pyinfra.__version__) < version.parse("3"):
        out.red("Please re-run scripts/initenv.sh to update pyinfra to version 3.")
        return 1

    try:
        out.check_call(cmd, env=env)
        if args.website_only:
            out.green("Website deployment completed.")
        elif not args.dns_check_disabled and strict_tls and not remote_data["acme_account_url"]:
            out.red("Deploy completed but letsencrypt not configured")
            out.red("Run 'cmdeploy run' again")
        else:
            out.green("Deploy completed, call `cmdeploy dns` next.")
        return 0
    except subprocess.CalledProcessError:
        out.red("Deploy failed")
        return 1


def adm_cmd_options(parser):
    parser.add_argument(
        "--version",
        default="latest",
        help="chatmail-control release tag to install, or 'latest'",
    )
    parser.add_argument(
        "--no-start",
        dest="start_service",
        action="store_false",
        default=True,
        help="install and enable chatmail-control without starting the service",
    )


def adm_cmd(args, out):
    """Install or upgrade Chatmail Control admin panel."""
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        out.red(
            "cmdeploy adm must be run as root because it writes to /etc and systemd."
        )
        return 1

    installer_url = (
        "https://raw.githubusercontent.com/localzet/chatmail-control/main/scripts/install.sh"
    )
    install_args = ["--version", args.version]
    if args.start_service:
        install_args.append("--start")

    public_url = f"https://admin.{args.config.mail_domain}"

    try:
        out.green(f"Downloading Chatmail Control installer from {installer_url}")
        with urllib.request.urlopen(installer_url, timeout=60) as response:
            installer = response.read()

        out.run(["bash", "-s", "--", *install_args], input=installer)
        update_chatmail_control_config(
            Path("/etc/chatmail-control/config.toml"),
            public_url=public_url,
            mail_domain=args.config.mail_domain,
        )
        out.green(f"Chatmail Control installed for {public_url}.")
        out.green(
            "Create the first admin with: "
            "chatmail-control admin create --username admin --password 'CHANGE_ME'"
        )
        return 0
    except (OSError, subprocess.CalledProcessError) as ex:
        out.red(ex)
        out.red("Chatmail Control installation failed")
        return 1


def update_chatmail_control_config(config_path, *, public_url, mail_domain):
    text = config_path.read_text()
    updates = {
        "server": {
            "public_url": public_url,
            "secure_cookies": True,
        },
        "auth": {},
        "health": {
            "domain": mail_domain,
        },
    }
    if "CHANGE_ME_64_RANDOM_CHARS" in text:
        updates["auth"]["session_secret"] = secrets.token_hex(32)

    lines = text.splitlines()
    result = []
    section = None
    seen = {name: set() for name in updates}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            append_missing_chatmail_control_settings(result, updates, seen, section)
            section = stripped.strip("[]")
            result.append(line)
            continue

        if section in updates and "=" in line and not stripped.startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates[section]:
                result.append(f"{key} = {render_toml_value(updates[section][key])}")
                seen[section].add(key)
                continue

        result.append(line)

    append_missing_chatmail_control_settings(result, updates, seen, section)
    config_path.write_text("\n".join(result) + "\n")


def append_missing_chatmail_control_settings(result, updates, seen, section):
    if section not in updates:
        return
    for key, value in updates[section].items():
        if key not in seen[section]:
            result.append(f"{key} = {render_toml_value(value)}")


def render_toml_value(value):
    if isinstance(value, bool):
        return str(value).lower()
    return repr(value).replace("'", '"')


def dns_cmd_options(parser):
    parser.add_argument(
        "--zonefile",
        dest="zonefile",
        type=pathlib.Path,
        default=None,
        help="write out a zonefile",
    )
    add_ssh_host_option(parser)


def dns_cmd(args, out):
    """Check DNS entries and optionally generate dns zone file."""
    ssh_host = args.ssh_host
    sshexec = get_sshexec(ssh_host, verbose=args.verbose)
    tls_cert_mode = args.config.tls_cert_mode
    strict_tls = tls_cert_mode == "acme"
    remote_data = dns.get_initial_remote_data(sshexec, args.config.mail_domain)
    if not dns.check_initial_remote_data(remote_data, strict_tls=strict_tls):
        return 1

    if strict_tls and not remote_data["acme_account_url"]:
        out.red("could not get letsencrypt account url, please run 'cmdeploy run'")
        return 1

    if not remote_data["dkim_entry"]:
        out.red("could not determine dkim_entry, please run 'cmdeploy run'")
        return 1

    remote_data["strict_tls"] = strict_tls
    zonefile = dns.get_filled_zone_file(remote_data)

    if args.zonefile:
        args.zonefile.write_text(zonefile)
        out.green(f"DNS records successfully written to: {args.zonefile}")
        return 0

    retcode = dns.check_full_zone(
        sshexec, remote_data=remote_data, zonefile=zonefile, out=out
    )
    return retcode


def status_cmd_options(parser):
    add_ssh_host_option(parser)


def status_cmd(args, out):
    """Display status for online chatmail instance."""

    ssh_host = args.ssh_host
    sshexec = get_sshexec(ssh_host, verbose=args.verbose)

    out.green(f"chatmail domain: {args.config.mail_domain}")
    if args.config.privacy_mail:
        out.green("privacy settings: present")
    else:
        out.red("no privacy settings")

    for line in sshexec(remote.rshell.get_systemd_running):
        print(line)


def test_cmd_options(parser):
    add_ssh_host_option(parser)


def test_cmd(args, out):
    """Run local and online tests for chatmail deployment."""

    env = os.environ.copy()
    env["CHATMAIL_INI"] = str(args.inipath.absolute())
    if args.ssh_host:
        env["CHATMAIL_SSH"] = args.ssh_host

    pytest_path = shutil.which("pytest")
    pytest_args = [
        pytest_path,
        "cmdeploy/src/",
        "-n4",
        "-rs",
        "-x",
        "-v",
        "--durations=5",
    ]
    ret = out.run_ret(pytest_args, env=env)
    return ret


def fmt_cmd_options(parser):
    parser.add_argument(
        "--check",
        "-c",
        action="store_true",
        help="only check but don't fix problems",
    )


def fmt_cmd(args, out):
    """Run formattting fixes on all chatmail source code."""

    chatmaild_dir = importlib.resources.files("chatmaild").resolve()
    cmdeploy_dir = chatmaild_dir.joinpath(
        "..", "..", "..", "cmdeploy", "src", "cmdeploy"
    ).resolve()
    sources = [str(chatmaild_dir), str(cmdeploy_dir)]

    format_args = [shutil.which("ruff"), "format"]
    check_args = [shutil.which("ruff"), "check"]

    if args.check:
        format_args.append("--diff")
    else:
        check_args.append("--fix")

    if not args.verbose:
        check_args.append("--quiet")
        format_args.append("--quiet")

    format_args.extend(sources)
    check_args.extend(sources)

    out.check_call(" ".join(format_args), quiet=not args.verbose)
    out.check_call(" ".join(check_args), quiet=not args.verbose)


def bench_cmd(args, out):
    """Run benchmarks against an online chatmail instance."""
    args = ["pytest", "--pyargs", "cmdeploy.tests.online.benchmark", "-vrx"]
    cmdstring = " ".join(args)
    out.green(f"[$ {cmdstring}]")
    subprocess.check_call(args)


def webdev_cmd(args, out):
    """Run local web development loop for static web pages."""
    from .www import main

    main()


#
# Parsing command line options and starting commands
#


class Out:
    """Convenience output printer providing coloring."""

    def red(self, msg, file=sys.stderr):
        print(colored(msg, "red"), file=file)

    def green(self, msg, file=sys.stderr):
        print(colored(msg, "green"), file=file)

    def __call__(self, msg, red=False, green=False, file=sys.stdout):
        color = "red" if red else ("green" if green else None)
        print(colored(msg, color), file=file)

    def check_call(self, arg, env=None, quiet=False):
        if not quiet:
            self(f"[$ {arg}]", file=sys.stderr)
        return subprocess.check_call(arg, shell=True, env=env)

    def run_ret(self, args, env=None, quiet=False):
        if not quiet:
            cmdstring = " ".join(args)
            self(f"[$ {cmdstring}]", file=sys.stderr)
        proc = subprocess.run(args, env=env, check=False)
        return proc.returncode

    def run(self, args, *, input=None, env=None, quiet=False):
        if not quiet:
            cmdstring = " ".join(args)
            self(f"[$ {cmdstring}]", file=sys.stderr)
        return subprocess.run(args, input=input, env=env, check=True)


def add_ssh_host_option(parser):
    parser.add_argument(
        "--ssh-host",
        dest="ssh_host",
        default="localhost",
        help="Run commands on 'localhost' or on a specific SSH host "
        "instead of chatmail.ini's mail_domain (default: localhost).",
    )


def add_config_option(parser):
    parser.add_argument(
        "--config",
        dest="inipath",
        action="store",
        default=Path(os.environ.get("CHATMAIL_INI", "chatmail.ini")),
        type=Path,
        help="path to the chatmail.ini file",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        dest="verbose",
        action="store_true",
        default=False,
        help="provide verbose logging",
    )


def add_subcommand(subparsers, func):
    name = func.__name__
    assert name.endswith("_cmd")
    name = name[:-4]
    doc = func.__doc__.strip()
    help = doc.split("\n")[0].strip(".")
    p = subparsers.add_parser(name, description=doc, help=help)
    p.set_defaults(func=func)
    add_config_option(p)
    return p


description = """
Setup your chatmail server configuration and
deploy it via SSH to your remote location.
"""


def get_parser():
    """Return an ArgumentParser for the 'cmdeploy' CLI"""

    parser = argparse.ArgumentParser(description=description.strip())
    subparsers = parser.add_subparsers(title="subcommands")

    # find all subcommands in the module namespace
    glob = globals()
    for name, func in glob.items():
        if name.endswith("_cmd"):
            subparser = add_subcommand(subparsers, func)
            addopts = glob.get(name + "_options")
            if addopts is not None:
                addopts(subparser)

    return parser


def get_sshexec(ssh_host: str, verbose=True):
    if ssh_host in ["localhost", "@local"]:
        return LocalExec(verbose)
    if verbose:
        print(f"[ssh] login to {ssh_host}")
    return SSHExec(ssh_host, verbose=verbose)


def main(args=None):
    """Provide main entry point for 'cmdeploy' CLI invocation."""
    parser = get_parser()
    args = parser.parse_args(args=args)
    if not hasattr(args, "func"):
        return parser.parse_args(["-h"])

    out = Out()
    kwargs = {}
    if args.func.__name__ not in ("init_cmd", "fmt_cmd"):
        if not args.inipath.exists():
            out.red(f"expecting {args.inipath} to exist, run init first?")
            raise SystemExit(1)
        try:
            args.config = read_config(args.inipath)
        except Exception as ex:
            out.red(ex)
            raise SystemExit(1)

    try:
        res = args.func(args, out, **kwargs)
        if res is None:
            res = 0
        return res
    except KeyboardInterrupt:
        out.red("KeyboardInterrupt")
        sys.exit(130)


if __name__ == "__main__":
    main()
