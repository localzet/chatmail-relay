"""
Pure python functions which execute remotely in a system Python interpreter.

All functions of this module

- need to get and and return Python builtin data types only,

- can only use standard library dependencies,

- can freely call each other.
"""

import re

from .rshell import CalledProcessError, log_progress, shell


def perform_initial_checks(mail_domain, pre_command=""):
    """Collecting initial DNS settings."""
    assert mail_domain
    if not shell("dig", fail_ok=True, print=log_progress):
        shell("apt-get update && apt-get install -y dnsutils", print=log_progress)
    A = query_dns("A", mail_domain)
    AAAA = query_dns("AAAA", mail_domain)
    MTA_STS = query_alias_or_same_address(
        f"mta-sts.{mail_domain}", mail_domain, A, AAAA
    )
    WWW = query_alias_or_same_address(f"www.{mail_domain}", mail_domain, A, AAAA)
    ADMIN = query_alias_or_same_address(
        f"admin.{mail_domain}", mail_domain, A, AAAA
    )

    res = dict(
        mail_domain=mail_domain,
        A=A,
        AAAA=AAAA,
        MTA_STS=MTA_STS,
        WWW=WWW,
        ADMIN=ADMIN,
    )
    res["acme_account_url"] = shell(
        pre_command + "acmetool account-url", fail_ok=True, print=log_progress
    )
    res["dkim_entry"], res["web_dkim_entry"] = get_dkim_entry(
        mail_domain, pre_command, dkim_selector="opendkim"
    )

    if not MTA_STS or not WWW or not ADMIN or (not A and not AAAA):
        return res

    # parse out sts-id if exists, example: "v=STSv1; id=2090123"
    mta_sts_txt = query_dns("TXT", f"_mta-sts.{mail_domain}")
    if not mta_sts_txt:
        return res
    parts = mta_sts_txt.split("id=")
    res["sts_id"] = parts[1].rstrip('"') if len(parts) == 2 else ""
    return res


def get_dkim_entry(mail_domain, pre_command, dkim_selector):
    try:
        dkim_pubkey = shell(
            f"{pre_command}openssl rsa -in /etc/dkimkeys/{dkim_selector}.private "
            "-pubout 2>/dev/null | awk '/-/{next}{printf(\"%s\",$0)}'",
            print=log_progress,
        )
    except CalledProcessError:
        return None, None
    dkim_value_raw = f"v=DKIM1;k=rsa;p={dkim_pubkey};s=email;t=s"
    dkim_value = '" "'.join(re.findall(".{1,255}", dkim_value_raw))
    web_dkim_value = "".join(re.findall(".{1,255}", dkim_value_raw))
    name = f"{dkim_selector}._domainkey.{mail_domain}."
    return (
        f'{name:<40} 3600   IN  TXT    "{dkim_value}"',
        f'{name:<40} 3600   IN  TXT    "{web_dkim_value}"',
    )


def query_dns(typ, domain):
    ns = query_authoritative_nameserver(domain)
    if not ns:
        return

    # Query authoritative nameserver directly to bypass DNS cache.
    res = shell(f"dig @{ns} -r -q {domain} -t {typ} +short", print=log_progress)
    return next((line for line in res.split("\n") if not line.startswith(";")), "")


def query_authoritative_nameserver(domain):
    labels = domain.rstrip(".").split(".")
    for index in range(len(labels) - 1):
        candidate = ".".join(labels[index:])
        soa = query_soa(candidate)
        if soa:
            return soa[4]
    return None


def query_soa(domain):
    answers = [
        x.split()
        for x in shell(
            f"dig -r -q {domain} -t SOA +noall +authority +answer",
            print=log_progress,
        ).split("\n")
    ]
    return next(
        (answer for answer in answers if len(answer) >= 5 and answer[3] == "SOA"),
        None,
    )


def query_alias_or_same_address(domain, target_domain, target_a, target_aaaa):
    cname = query_dns("CNAME", domain)
    if cname:
        return cname

    domain_a = query_dns("A", domain)
    domain_aaaa = query_dns("AAAA", domain)
    if (target_a and domain_a == target_a) or (
        target_aaaa and domain_aaaa == target_aaaa
    ):
        return f"{target_domain}."

    return ""


def cname_or_same_address_matches(domain, target_domain):
    target_domain = target_domain.rstrip(".")
    target_a = query_dns("A", target_domain)
    target_aaaa = query_dns("AAAA", target_domain)
    return query_alias_or_same_address(domain, target_domain, target_a, target_aaaa) == (
        f"{target_domain}."
    )


def check_zonefile(zonefile, verbose=True):
    """Check expected zone file entries."""
    required = True
    required_diff = []
    recommended_diff = []

    for zf_line in zonefile.splitlines():
        if "; Recommended" in zf_line:
            required = False
            continue
        if not zf_line.strip() or zf_line.startswith(";"):
            continue
        print(f"dns-checking {zf_line!r}") if verbose else log_progress("")
        zf_domain, _ttl, _in, zf_typ, zf_value = zf_line.split(None, 4)
        zf_domain = zf_domain.rstrip(".")
        zf_value = zf_value.strip()
        query_value = query_dns(zf_typ, zf_domain)
        if zf_value != query_value and not (
            zf_typ == "CNAME" and cname_or_same_address_matches(zf_domain, zf_value)
        ):
            assert zf_typ in ("A", "AAAA", "CNAME", "CAA", "SRV", "MX", "TXT"), zf_line
            if required:
                required_diff.append(zf_line)
            else:
                recommended_diff.append(zf_line)

    return required_diff, recommended_diff
